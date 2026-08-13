"""Filler -- a two player GUI for the GamePigeon game, backed by the C++ solver.

Run it with:   py filler.py

The board is drawn by pygame and all game rules are enforced here in Python.
The C++ program in solver.cpp is only used as an oracle: after every move the
full position is handed to it and it answers with the perfect-play best move
for *both* players, so the recommendation is on screen no matter whose turn it
is.  solver.cpp is compiled automatically the first time it is needed (and
again whenever it changes), then kept alive as a single background process so
there is no per-move startup cost.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pygame

# --------------------------------------------------------------------------
# board / colors
# --------------------------------------------------------------------------

W, H = 8, 7
NCELLS = W * H

EMPTY = -1

# Index order matches solver.cpp: i = y * W + x, with y = 0 being the bottom row.
PALETTE = [
    (228, 74, 95),    # 0 red
    (163, 209, 74),   # 1 green
    (245, 222, 63),   # 2 yellow
    (79, 168, 238),   # 3 blue
    (99, 64, 158),    # 4 purple
    (61, 61, 61),     # 5 grey
]
COLOR_NAMES = ["Red", "Green", "Yellow", "Blue", "Purple", "Grey"]

BG = (237, 238, 240)
CARD = (250, 250, 251)
INK = (32, 34, 38)
MUTED = (126, 131, 140)
LINE = (222, 224, 228)
ACCENT = (60, 132, 232)

# Territory outline: (keyline, body).  The keyline is dark for both players so
# the border reads against the pale board, and the body is what tells them
# apart -- player 1 solid ink, player 2 a white line with dark edging.
OWNER_STROKE = [
    ((22, 24, 29), (22, 24, 29)),
    ((22, 24, 29), (250, 250, 252)),
]
STROKE_BAND = 5   # total outline width, measured inward from the seam
STROKE_KEY = 1    # keyline width on each side of the body
PLAYER_NAMES = ["Player 1", "Player 2"]

# --------------------------------------------------------------------------
# layout
# --------------------------------------------------------------------------

TILE = 66
GAP = 5
BOARD_X, BOARD_Y = 46, 160
BOARD_W = W * TILE + (W - 1) * GAP
BOARD_H = H * TILE + (H - 1) * GAP

EVAL_H = 18
EVAL_Y = BOARD_Y - 48

PANEL_X = BOARD_X + BOARD_W + 40
PANEL_W = 380

SWATCH = 62
SWATCH_GAP = (BOARD_W - 6 * SWATCH) // 5
PALETTE_Y = BOARD_Y + BOARD_H + 40

WIN_W = PANEL_X + PANEL_W + 46
WIN_H = PALETTE_Y + SWATCH + 62


def cell_rect(x: int, y: int) -> pygame.Rect:
    return pygame.Rect(
        BOARD_X + x * (TILE + GAP),
        BOARD_Y + (H - 1 - y) * (TILE + GAP),
        TILE,
        TILE,
    )


def slot_rect(x: int, y: int) -> pygame.Rect:
    """A tile plus half the gap on every side.  Neighbouring slots share an
    edge exactly, which is what lets territory outlines join up."""
    return pygame.Rect(
        BOARD_X + x * (TILE + GAP) - GAP // 2,
        BOARD_Y + (H - 1 - y) * (TILE + GAP) - GAP // 2,
        TILE + GAP,
        TILE + GAP,
    )


def swatch_rect(c: int) -> pygame.Rect:
    return pygame.Rect(BOARD_X + c * (SWATCH + SWATCH_GAP), PALETTE_Y, SWATCH, SWATCH)


# --------------------------------------------------------------------------
# game rules
# --------------------------------------------------------------------------

NEIGHBOURS = ((1, 0), (-1, 0), (0, 1), (0, -1))

# The two cells each player's home corner touches, in fill order.
HOME_NEIGHBOURS = (
    ((1, 0), (0, 1)),                    # player 1, bottom left
    ((W - 1, H - 2), (W - 2, H - 1)),    # player 2, top right
)


def random_board() -> list[list[int]]:
    """A legal Filler board: no two orthogonally adjacent cells share a color,
    and neither home corner has both of its neighbours in the same color.

    The two home corners are allowed to match -- whoever moves first simply
    leaves that color behind on their opening turn.
    """
    while True:
        board = [[EMPTY] * W for _ in range(H)]
        ok = True

        for y in range(H):
            for x in range(W):
                banned = set()
                if x:
                    banned.add(board[y][x - 1])
                if y:
                    banned.add(board[y - 1][x])

                # A corner touches exactly two cells.  If those match, that
                # player opens facing a single color and has just one capture
                # to choose from.
                if (x, y) == HOME_NEIGHBOURS[0][1]:
                    banned.add(board[0][1])
                elif (x, y) == HOME_NEIGHBOURS[1][1]:
                    banned.add(board[H - 2][W - 1])

                choices = [c for c in range(6) if c not in banned]

                if not choices:
                    ok = False
                    break

                board[y][x] = random.choice(choices)

            if not ok:
                break

        if ok:
            return board


def validate_board(board: list[list[int]]) -> list[str]:
    problems = []

    empty = sum(1 for row in board for c in row if c == EMPTY)
    if empty:
        problems.append(f"{empty} tile{'s' if empty != 1 else ''} still empty")

    clashes = 0
    for y in range(H):
        for x in range(W):
            if board[y][x] == EMPTY:
                continue
            if x + 1 < W and board[y][x] == board[y][x + 1]:
                clashes += 1
            if y + 1 < H and board[y][x] == board[y + 1][x]:
                clashes += 1

    if clashes:
        problems.append(f"{clashes} pair{'s' if clashes != 1 else ''} of touching tiles share a color")

    for pid, ((ax, ay), (bx, by)) in enumerate(HOME_NEIGHBOURS):
        first, second = board[ay][ax], board[by][bx]
        if first != EMPTY and first == second:
            problems.append(f"both tiles beside player {pid + 1}'s corner are the same color")

    return problems


class Game:
    def __init__(self, board: list[list[int]], first: int = 0):
        self.colors = [row[:] for row in board]
        self.owner = [[EMPTY] * W for _ in range(H)]
        self.owner[0][0] = 0
        self.owner[H - 1][W - 1] = 1

        self.player_color = [self.colors[0][0], self.colors[H - 1][W - 1]]
        self.turn = first
        self.history: list[tuple] = []

    # -- queries ---------------------------------------------------------

    def legal_colors(self) -> list[int]:
        return [c for c in range(6) if c not in self.player_color]

    def capture(self, pid: int, color: int) -> set[tuple[int, int]]:
        """Cells the given player would claim by switching to `color`."""
        if color in self.player_color:
            return set()

        stack = [
            (x, y)
            for y in range(H)
            for x in range(W)
            if self.owner[y][x] == pid
        ]
        gained: set[tuple[int, int]] = set()

        while stack:
            x, y = stack.pop()
            for dx, dy in NEIGHBOURS:
                nx, ny = x + dx, y + dy
                if not (0 <= nx < W and 0 <= ny < H):
                    continue
                if self.owner[ny][nx] != EMPTY or (nx, ny) in gained:
                    continue
                if self.colors[ny][nx] != color:
                    continue
                gained.add((nx, ny))
                stack.append((nx, ny))

        return gained

    def is_over(self) -> bool:
        return all(o != EMPTY for row in self.owner for o in row)

    def score(self, pid: int) -> int:
        return sum(1 for row in self.owner for o in row if o == pid)

    # -- mutation --------------------------------------------------------

    def snapshot(self) -> tuple:
        return (
            [row[:] for row in self.colors],
            [row[:] for row in self.owner],
            list(self.player_color),
            self.turn,
        )

    def restore(self, snap: tuple) -> None:
        self.colors = [row[:] for row in snap[0]]
        self.owner = [row[:] for row in snap[1]]
        self.player_color = list(snap[2])
        self.turn = snap[3]

    def play(self, color: int) -> set[tuple[int, int]]:
        pid = self.turn
        gained = self.capture(pid, color)
        self.history.append(self.snapshot())

        for x, y in gained:
            self.owner[y][x] = pid

        for y in range(H):
            for x in range(W):
                if self.owner[y][x] == pid:
                    self.colors[y][x] = color

        self.player_color[pid] = color
        self.turn = 1 - pid
        return gained

    def undo(self) -> bool:
        if not self.history:
            return False
        self.restore(self.history.pop())
        return True

    # -- solver payload --------------------------------------------------

    def encode(self) -> tuple[str, str, int, int, int]:
        colors = "".join(str(self.colors[y][x]) for y in range(H) for x in range(W))
        owners = "".join(".01"[self.owner[y][x] + 1] for y in range(H) for x in range(W))
        return colors, owners, self.player_color[0], self.player_color[1], self.turn


# --------------------------------------------------------------------------
# solver process
# --------------------------------------------------------------------------

class SolverError(Exception):
    pass


class Solver:
    """Compiles solver.cpp on demand and keeps one process alive for queries."""

    def __init__(self, root: Path):
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.env = dict(os.environ)

    @property
    def exe(self) -> Path:
        return self.root / ("solver.exe" if os.name == "nt" else "solver")

    def _use_compiler(self, cc: str) -> None:
        """Put the compiler's own directory first on PATH.

        A second MinGW install earlier on PATH (Git for Windows ships one) can
        shadow the runtime DLLs g++ needs, and it then fails with exit code 1
        and no diagnostics at all.  Leading with its own bin directory avoids
        that, both for the build and for anything it produces.
        """
        self.env["PATH"] = os.path.dirname(cc) + os.pathsep + self.env.get("PATH", "")

    def _build(self) -> Path:
        src = self.root / "solver.cpp"
        if not src.exists():
            raise SolverError("solver.cpp is not next to filler.py")

        if self.exe.exists() and self.exe.stat().st_mtime >= src.stat().st_mtime:
            return self.exe

        cc = shutil.which("g++") or shutil.which("clang++")
        if cc is None:
            if self.exe.exists():
                return self.exe
            raise SolverError("no g++ or clang++ on PATH, and solver is not built")

        self._use_compiler(cc)
        base = [cc, "-O2", "-std=c++20"]
        static = ["-static", "-static-libgcc", "-static-libstdc++"]
        tail = ["-o", str(self.exe), str(src)]

        failure = ""
        for flags in (base + static + tail, base + tail):
            result = subprocess.run(flags, capture_output=True, text=True, env=self.env)
            if result.returncode == 0:
                return self.exe
            failure = (result.stderr or result.stdout).strip() or (
                f"{Path(cc).name} exited with code {result.returncode} and said nothing"
            )

        raise SolverError("compile failed:\n" + failure)

    def _ensure(self) -> subprocess.Popen:
        if self.proc is not None and self.proc.poll() is None:
            return self.proc

        exe = self._build()
        self.proc = subprocess.Popen(
            [str(exe)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=self.env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return self.proc

    def solve(self, colors: str, owners: str, p1_color: int, p2_color: int, side: int) -> dict:
        proc = self._ensure()

        try:
            proc.stdin.write(f"SOLVE\n{colors}\n{owners}\n{p1_color} {p2_color} {side}\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.proc = None
            raise SolverError(f"solver died: {exc}") from exc

        head = proc.stdout.readline().strip()
        if not head:
            self.proc = None
            raise SolverError("solver closed its output")
        if head.startswith("ERR"):
            raise SolverError(head[4:].strip())

        out = {"best": -1, "scores": {}, "captures": {}, "nodes": 0, "ms": 0, "side": side}

        while True:
            line = proc.stdout.readline()
            if not line:
                self.proc = None
                raise SolverError("solver closed its output")

            line = line.strip()
            if line == "END":
                break

            parts = line.split()
            if parts[0] == "BEST":
                out["best"] = int(parts[1])
                if len(parts) > 2 and parts[2] != "-":
                    for chunk in parts[2].split(","):
                        c, s, gain = chunk.split(":")
                        out["scores"][int(c)] = int(s)
                        out["captures"][int(c)] = int(gain)
            elif parts[0] == "NODES":
                out["nodes"] = int(parts[1])
                out["ms"] = int(parts[2])

        return out

    def shutdown(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.write("QUIT\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=1)
        except Exception:
            self.proc.kill()


class Analyst:
    """Runs solver queries off the main thread so the window never freezes.

    Only the newest request matters; older ones are dropped on the floor.
    """

    def __init__(self, solver: Solver):
        self.solver = solver
        self.cv = threading.Condition()
        self.pending: tuple | None = None
        self.generation = 0
        self.result: dict | None = None
        self.result_gen = -1
        self.error = ""
        self.busy = False
        self.stop = False

        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, payload: tuple) -> None:
        with self.cv:
            self.generation += 1
            self.pending = (self.generation, payload)
            self.result = None
            self.error = ""
            self.busy = True
            self.cv.notify()

    def clear(self) -> None:
        with self.cv:
            self.generation += 1
            self.pending = None
            self.result = None
            self.error = ""
            self.busy = False

    def snapshot(self) -> tuple[dict | None, str, bool]:
        with self.cv:
            fresh = self.result if self.result_gen == self.generation else None
            return fresh, self.error, self.busy

    def _loop(self) -> None:
        while True:
            with self.cv:
                while self.pending is None and not self.stop:
                    self.cv.wait()
                if self.stop:
                    return
                gen, payload = self.pending
                self.pending = None

            try:
                result = self.solver.solve(*payload)
                error = ""
            except SolverError as exc:
                result, error = None, str(exc)
            except Exception as exc:  # noqa: BLE001 - surface anything in the UI
                result, error = None, f"{type(exc).__name__}: {exc}"

            with self.cv:
                if gen == self.generation:
                    self.result = result
                    self.result_gen = gen
                    self.error = error
                    self.busy = self.pending is not None


# --------------------------------------------------------------------------
# drawing helpers
# --------------------------------------------------------------------------

_shadow_cache: dict[tuple, pygame.Surface] = {}


def soft_shadow(size: tuple[int, int], radius: int, spread: int = 10, alpha: int = 30):
    key = (size, radius, spread, alpha)
    if key not in _shadow_cache:
        w, h = size
        surf = pygame.Surface((w + spread * 2, h + spread * 2), pygame.SRCALPHA)
        for i in range(spread, 0, -1):
            a = int(alpha * (1 - i / (spread + 1)) ** 1.6) + 2
            pygame.draw.rect(
                surf,
                (0, 0, 0, a),
                (spread - i, spread - i + 2, w + i * 2, h + i * 2),
                border_radius=radius + i,
            )
        _shadow_cache[key] = surf
    return _shadow_cache[key]


def card(surf, rect, radius=16, fill=CARD, shadow=True):
    if shadow:
        sh = soft_shadow((rect.width, rect.height), radius)
        surf.blit(sh, (rect.left - 10, rect.top - 10))
    pygame.draw.rect(surf, fill, rect, border_radius=radius)


def text_lines(fnt, text, width):
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if fnt.size(candidate)[0] <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def readable_on(color) -> tuple[int, int, int]:
    r, g, b = color
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    return (24, 24, 28) if luma > 150 else (255, 255, 255)


class Button:
    def __init__(self, rect, label, kind="normal"):
        self.rect = pygame.Rect(rect)
        self.label = label
        self.kind = kind
        self.enabled = True
        self.hover = False
        self.selected = False

    def draw(self, surf, fnt):
        if self.kind == "toggle":
            base = ACCENT if self.selected else (255, 255, 255)
            fg = (255, 255, 255) if self.selected else INK
        elif self.kind == "primary":
            base = ACCENT if self.enabled else (198, 205, 216)
            fg = (255, 255, 255)
        else:
            base = (255, 255, 255) if self.enabled else (244, 245, 247)
            fg = INK if self.enabled else (183, 188, 196)

        if self.enabled and self.hover:
            lift = -18 if (self.kind == "primary" or self.selected) else 18
            base = tuple(max(0, min(255, v + lift)) for v in base)

        pygame.draw.rect(surf, base, self.rect, border_radius=10)
        if self.kind != "primary" and not self.selected:
            pygame.draw.rect(surf, LINE, self.rect, width=1, border_radius=10)

        label = fnt.render(self.label, True, fg)
        surf.blit(label, label.get_rect(center=self.rect.center))

    def hit(self, pos) -> bool:
        return self.enabled and self.rect.collidepoint(pos)


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------

SETUP, PLAY = "setup", "play"


class App:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("Filler")
        self.screen = pygame.display.set_mode((WIN_W, WIN_H))
        self.clock = pygame.time.Clock()

        family = "Segoe UI,Helvetica Neue,Arial,DejaVu Sans"
        self.f_title = pygame.font.SysFont(family, 30, bold=True)
        self.f_head = pygame.font.SysFont(family, 19, bold=True)
        self.f_body = pygame.font.SysFont(family, 17)
        self.f_small = pygame.font.SysFont(family, 14)
        self.f_chip = pygame.font.SysFont(family, 24, bold=True)
        self.f_badge = pygame.font.SysFont(family, 13, bold=True)

        root = Path(__file__).resolve().parent
        self.solver = Solver(root)
        self.analyst = Analyst(self.solver)

        self.mode = SETUP
        self.board = random_board()
        self.first_player = 0
        self.paint_color = 0
        self.painting = False
        self.erasing = False
        self.game: Game | None = None
        self.flash: dict[tuple[int, int], float] = {}
        self.message = ""
        self._territory_key: tuple | None = None
        self._territory_surf = pygame.Surface((1, 1), pygame.SRCALPHA)
        self._eval_target = 0.5   # player 1's predicted share of the board
        self._eval_share = 0.5    # what is drawn, eased toward the target

        self.btn_random = Button((PANEL_X, 292, 180, 44), "Randomize")
        self.btn_clear = Button((PANEL_X + 200, 292, 180, 44), "Clear")
        self.btn_first = [
            Button((PANEL_X, 382, 186, 42), "Player 1", "toggle"),
            Button((PANEL_X + 194, 382, 186, 42), "Player 2", "toggle"),
        ]
        self.btn_start = Button((PANEL_X, 532, PANEL_W, 52), "Start game", "primary")
        self.btn_undo = Button((PANEL_X, 556, 180, 44), "Undo")
        self.btn_edit = Button((PANEL_X + 200, 556, 180, 44), "Edit board")
        self.btn_new = Button((PANEL_X, 612, PANEL_W, 44), "New random board")

    # -- solver plumbing -------------------------------------------------

    def request_analysis(self):
        if self.game is None or self.game.is_over():
            self.analyst.clear()
            return
        self.analyst.submit(self.game.encode())

    # -- mode transitions ------------------------------------------------

    def start_game(self):
        problems = validate_board(self.board)
        if problems:
            self.message = problems[0]
            return
        self.game = Game(self.board, self.first_player)
        self.mode = PLAY
        self.message = ""
        self.flash = {}
        self._eval_target = self._eval_share = 0.5
        self.request_analysis()

    def back_to_setup(self):
        if self.game is not None:
            # keep the untouched starting layout, not the played-on one
            snap = self.game.history[0] if self.game.history else None
            if snap is not None:
                self.board = [row[:] for row in snap[0]]
        self.mode = SETUP
        self.game = None
        self.analyst.clear()
        self.message = ""

    def do_move(self, color: int):
        assert self.game is not None
        if color in self.game.player_color:
            return
        gained = self.game.play(color)
        now = time.time()
        self.flash = {cell: now for cell in gained}
        self.request_analysis()

    # -- events ----------------------------------------------------------

    def handle_event(self, event):
        if event.type == pygame.QUIT:
            return False

        if event.type == pygame.KEYDOWN:
            self.handle_key(event)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            self.handle_click(event)
        elif event.type == pygame.MOUSEBUTTONUP:
            self.painting = self.erasing = False
        elif event.type == pygame.MOUSEMOTION and self.mode == SETUP:
            if self.painting or self.erasing:
                self.paint_at(event.pos, EMPTY if self.erasing else self.paint_color)

        return True

    def handle_key(self, event):
        if event.key == pygame.K_ESCAPE:
            pygame.event.post(pygame.event.Event(pygame.QUIT))
            return

        if pygame.K_1 <= event.key <= pygame.K_6:
            index = event.key - pygame.K_1
            if self.mode == SETUP:
                self.paint_color = index
            elif self.game is not None and index not in self.game.player_color:
                self.do_move(index)
            return

        if self.mode == SETUP:
            if event.key == pygame.K_r:
                self.board = random_board()
                self.message = ""
            elif event.key == pygame.K_c:
                self.board = [[EMPTY] * W for _ in range(H)]
                self.message = ""
            elif event.key == pygame.K_s:
                self.first_player = 1 - self.first_player
                self.message = ""
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.start_game()
            return

        if self.game is None:
            return

        if event.key == pygame.K_u:
            if self.game.undo():
                self.flash = {}
                self.request_analysis()
        elif event.key == pygame.K_e:
            self.back_to_setup()
        elif event.key == pygame.K_SPACE:
            result, _, _ = self.analyst.snapshot()
            if result and not self.game.is_over() and result["best"] >= 0:
                self.do_move(result["best"])

    def handle_click(self, event):
        pos = event.pos

        if event.button in (4, 5):
            return

        for c in range(6):
            if swatch_rect(c).collidepoint(pos):
                if self.mode == SETUP:
                    self.paint_color = c
                elif self.game is not None and c not in self.game.player_color:
                    self.do_move(c)
                return

        if self.mode == SETUP:
            for pid, button in enumerate(self.btn_first):
                if button.hit(pos):
                    self.first_player = pid
                    self.message = ""
                    return

            if self.btn_random.hit(pos):
                self.board = random_board()
                self.message = ""
                return
            if self.btn_clear.hit(pos):
                self.board = [[EMPTY] * W for _ in range(H)]
                self.message = ""
                return
            if self.btn_start.hit(pos):
                self.start_game()
                return

            if event.button == 1:
                self.painting = True
                self.paint_at(pos, self.paint_color)
            elif event.button == 3:
                self.erasing = True
                self.paint_at(pos, EMPTY)
            return

        if self.game is not None:
            if self.btn_undo.hit(pos):
                if self.game.undo():
                    self.flash = {}
                    self.request_analysis()
            elif self.btn_edit.hit(pos):
                self.back_to_setup()
            elif self.btn_new.hit(pos):
                self.board = random_board()
                self.start_game()

    def paint_at(self, pos, color):
        for y in range(H):
            for x in range(W):
                if cell_rect(x, y).collidepoint(pos):
                    self.board[y][x] = color
                    self.message = ""
                    return

    def hovered_cell(self, pos):
        for y in range(H):
            for x in range(W):
                if cell_rect(x, y).collidepoint(pos):
                    return x, y
        return None

    # -- drawing ---------------------------------------------------------

    def run(self):
        running = True
        while running:
            for event in pygame.event.get():
                running = self.handle_event(event)
                if not running:
                    break

            self.draw()
            self.clock.tick(60)

        self.solver.shutdown()
        pygame.quit()

    def draw(self):
        mouse = pygame.mouse.get_pos()
        self.screen.fill(BG)

        for btn in (self.btn_random, self.btn_clear, self.btn_start,
                    self.btn_undo, self.btn_edit, self.btn_new, *self.btn_first):
            btn.hover = btn.rect.collidepoint(mouse)

        result, error, busy = self.analyst.snapshot()

        self.draw_header(result)
        self.draw_eval_bar(result)
        self.draw_board(mouse)
        self.draw_palette(mouse, result)

        if self.mode == SETUP:
            self.draw_setup_panel()
        else:
            self.draw_play_panel(result, error, busy)

        pygame.display.flip()

    def draw_header(self, result):
        title = self.f_title.render("Filler", True, INK)
        self.screen.blit(title, (BOARD_X, 38))

        subtitle = "Draw the starting board" if self.mode == SETUP else "Perfect-play advice, live"
        self.screen.blit(self.f_small.render(subtitle.upper(), True, MUTED), (BOARD_X + 4, 78))

        if self.game is None:
            return

        scores = [self.game.score(0), self.game.score(1)]
        chip_w, chip_h = 74, 44
        chip_x = BOARD_X + BOARD_W - (2 * chip_w + 14)
        for pid in (0, 1):
            colour = PALETTE[self.game.player_color[pid]]
            rect = pygame.Rect(chip_x + pid * (chip_w + 14), 36, chip_w, chip_h)
            pygame.draw.rect(self.screen, colour, rect, border_radius=10)

            if self.mode == PLAY and self.game.turn == pid and not self.game.is_over():
                pygame.draw.rect(self.screen, INK, rect.inflate(8, 8), width=3, border_radius=13)

            label = self.f_chip.render(f"{scores[pid]:02d}", True, readable_on(colour))
            self.screen.blit(label, label.get_rect(center=rect.center))

            tag = self.f_badge.render(f"P{pid + 1}", True, MUTED)
            self.screen.blit(tag, tag.get_rect(midtop=(rect.centerx, rect.bottom + 4)))

    def draw_eval_bar(self, result):
        """Chess-style eval bar showing the predicted final split of the board.

        A draw sits dead centre; player 1 winning by 8 means they finish with
        32 of the 56 tiles, so they take 32/56ths of the bar.
        """
        if self.game is None:
            return

        if self.game.is_over():
            target = self.game.score(0) / NCELLS
        elif result is not None and result["best"] in result["scores"]:
            margin = result["scores"][result["best"]]
            target = (NCELLS + margin) / (2 * NCELLS)
        else:
            target = self._eval_target   # hold steady while the solver thinks

        self._eval_target = target
        self._eval_share += (target - self._eval_share) * 0.2

        rect = pygame.Rect(BOARD_X, EVAL_Y, BOARD_W, EVAL_H)
        radius = EVAL_H // 2
        split = rect.left + int(round(rect.width * self._eval_share))

        # player 2 fills the whole bar, then player 1 is painted over the left
        # of it through a clip, so both ends keep the same rounded silhouette
        pygame.draw.rect(self.screen, PALETTE[self.game.player_color[1]], rect,
                         border_radius=radius)

        if split > rect.left:
            clip = self.screen.get_clip()
            self.screen.set_clip(pygame.Rect(rect.left, rect.top, split - rect.left, rect.height))
            pygame.draw.rect(self.screen, PALETTE[self.game.player_color[0]], rect,
                             border_radius=radius)
            self.screen.set_clip(clip)

        # keeps the halves apart even when both players are on the same color
        if rect.left < split < rect.right:
            pygame.draw.rect(self.screen, INK, (split - 1, rect.top, 2, rect.height))

    def draw_board(self, mouse):
        panel = pygame.Rect(BOARD_X - 14, BOARD_Y - 14, BOARD_W + 28, BOARD_H + 28)
        card(self.screen, panel, radius=18)

        preview: set[tuple[int, int]] = set()
        if self.mode == PLAY and self.game is not None and not self.game.is_over():
            for c in range(6):
                if swatch_rect(c).collidepoint(mouse) and c not in self.game.player_color:
                    preview = self.game.capture(self.game.turn, c)

        board = self.game.colors if self.game is not None else self.board
        hovered = self.hovered_cell(mouse) if self.mode == SETUP else None
        now = time.time()

        for y in range(H):
            for x in range(W):
                rect = cell_rect(x, y)
                value = board[y][x]

                if value == EMPTY:
                    pygame.draw.rect(self.screen, (236, 238, 241), rect, border_radius=6)
                    pygame.draw.rect(self.screen, (214, 217, 223), rect, width=2, border_radius=6)
                else:
                    colour = PALETTE[value]
                    age = now - self.flash.get((x, y), -9)
                    if 0 <= age < 0.45:
                        t = 1 - age / 0.45
                        colour = tuple(int(v + (255 - v) * 0.55 * t) for v in colour)
                    pygame.draw.rect(self.screen, colour, rect, border_radius=6)

                if hovered == (x, y):
                    pygame.draw.rect(self.screen, INK, rect.inflate(6, 6), width=3, border_radius=8)

                if (x, y) in preview:
                    pulse = 0.5 + 0.5 * abs(((now * 2) % 2) - 1)
                    ring = tuple(int(255 * pulse + 30 * (1 - pulse)) for _ in range(3))
                    pygame.draw.rect(self.screen, ring, rect, width=4, border_radius=6)

        if self.game is not None:
            self.draw_territory()

    def region_rects(self, pid: int, inset: int, origin: tuple[int, int]) -> list[pygame.Rect]:
        """The player's territory as slot rects, pulled in by `inset` on every
        side that faces something they don't own.

        Slots tile the board edge to edge (each covers its tile plus half the
        surrounding gap), so the union of these rects is the region shrunk by
        `inset` -- with no seams where two owned tiles meet.
        """
        assert self.game is not None
        owner = self.game.owner
        ox, oy = origin
        rects = []

        for y in range(H):
            for x in range(W):
                if owner[y][x] != pid:
                    continue

                s = slot_rect(x, y)
                left = s.left + (0 if x > 0 and owner[y][x - 1] == pid else inset)
                right = s.right - (0 if x + 1 < W and owner[y][x + 1] == pid else inset)
                top = s.top + (0 if y + 1 < H and owner[y + 1][x] == pid else inset)
                bottom = s.bottom - (0 if y > 0 and owner[y - 1][x] == pid else inset)

                if right > left and bottom > top:
                    rects.append(pygame.Rect(left - ox, top - oy, right - left, bottom - top))

        return rects

    def territory_surface(self) -> pygame.Surface:
        """One continuous outline per player, drawn as nested rings.

        Each ring is the whole region shrunk by a fixed amount, painted over
        the previous one, so the outline follows the silhouette of the
        territory instead of being stitched together from per-tile edges.
        """
        assert self.game is not None

        signature = tuple(tuple(row) for row in self.game.owner)
        if self._territory_key == signature:
            return self._territory_surf

        origin = (BOARD_X - GAP // 2, BOARD_Y - GAP // 2)
        surf = pygame.Surface((BOARD_W + GAP, BOARD_H + GAP), pygame.SRCALPHA)

        for pid in (0, 1):
            key_col, body_col = OWNER_STROKE[pid]
            for inset, colour in (
                (0, key_col),                       # outer keyline
                (STROKE_KEY, body_col),             # the line itself
                (STROKE_BAND - STROKE_KEY, key_col),  # inner keyline
                (STROKE_BAND, (0, 0, 0, 0)),        # punch out the middle
            ):
                for rect in self.region_rects(pid, inset, origin):
                    surf.fill(colour, rect)

        self._territory_key = signature
        self._territory_surf = surf
        return surf

    def draw_territory(self):
        self.screen.blit(self.territory_surface(), (BOARD_X - GAP // 2, BOARD_Y - GAP // 2))

    def draw_palette(self, mouse, result):
        for c in range(6):
            rect = swatch_rect(c)
            locked = self.game is not None and c in self.game.player_color
            hover = rect.collidepoint(mouse)

            draw_rect = rect if (locked or not hover) else rect.inflate(6, 6)
            colour = PALETTE[c]
            if locked:
                colour = tuple(int(v * 0.35 + 232 * 0.65) for v in colour)

            pygame.draw.rect(self.screen, colour, draw_rect, border_radius=10)

            if self.mode == SETUP and c == self.paint_color:
                pygame.draw.rect(self.screen, INK, rect.inflate(10, 10), width=3, border_radius=13)

            if locked:
                # both players can sit on one color if the home corners matched
                who = " ".join(f"P{p + 1}" for p in (0, 1) if self.game.player_color[p] == c)
                tag = self.f_badge.render(who, True, (120, 125, 134))
                self.screen.blit(tag, tag.get_rect(center=rect.center))
                continue

            if self.mode == PLAY and self.game is not None and not self.game.is_over():
                if result and result["best"] == c:
                    pygame.draw.rect(self.screen, INK, rect.inflate(14, 14), width=3, border_radius=15)
                    star = self.f_badge.render("BEST", True, INK)
                    self.screen.blit(star, star.get_rect(midtop=(rect.centerx, rect.bottom + 12)))

            key = self.f_badge.render(str(c + 1), True, MUTED)
            self.screen.blit(key, key.get_rect(midbottom=(rect.centerx, rect.top - 6)))

    def draw_setup_panel(self):
        rect = pygame.Rect(PANEL_X - 14, BOARD_Y - 14, PANEL_W + 28, 604)
        card(self.screen, rect, radius=18)

        y = BOARD_Y + 8
        self.screen.blit(self.f_head.render("Board setup", True, INK), (PANEL_X, y))
        y += 34

        body = (
            "Pick a color below, then click or drag on the board to paint it. "
            "Right click erases a tile. Player 1 starts in the bottom left corner, "
            "player 2 in the top right."
        )
        for line in text_lines(self.f_body, body, PANEL_W):
            self.screen.blit(self.f_body.render(line, True, MUTED), (PANEL_X, y))
            y += 23

        self.btn_random.draw(self.screen, self.f_body)
        self.btn_clear.draw(self.screen, self.f_body)

        self.screen.blit(self.f_badge.render("WHO MOVES FIRST", True, MUTED), (PANEL_X, 356))
        for pid, button in enumerate(self.btn_first):
            button.selected = pid == self.first_player
            button.draw(self.screen, self.f_body)

        y = 444
        problems = validate_board(self.board)
        if problems:
            self.screen.blit(self.f_head.render("Not ready yet", True, (196, 64, 76)), (PANEL_X, y))
            y += 30
            for problem in problems:
                for line in text_lines(self.f_body, "- " + problem, PANEL_W):
                    self.screen.blit(self.f_body.render(line, True, MUTED), (PANEL_X, y))
                    y += 22
        else:
            self.screen.blit(self.f_head.render("Board is legal", True, (46, 140, 86)), (PANEL_X, y))
            y += 30
            note = (
                f"Player 1 opens on {COLOR_NAMES[self.board[0][0]]}, "
                f"player 2 on {COLOR_NAMES[self.board[H - 1][W - 1]]}. "
                f"{PLAYER_NAMES[self.first_player]} moves first."
            )
            for line in text_lines(self.f_body, note, PANEL_W):
                self.screen.blit(self.f_body.render(line, True, MUTED), (PANEL_X, y))
                y += 22

        self.btn_start.enabled = not problems
        self.btn_start.draw(self.screen, self.f_head)

        if self.message:
            msg = self.f_small.render(self.message, True, (196, 64, 76))
            self.screen.blit(msg, (PANEL_X, self.btn_start.rect.bottom + 12))

        hints = ("R randomize    C clear    S swap who starts",
                 "1-6 pick color    Enter start")
        top = rect.bottom - 34 - (len(hints) - 1) * 18
        for line in hints:
            self.screen.blit(self.f_small.render(line, True, MUTED), (PANEL_X, top))
            top += 18

    def draw_play_panel(self, result, error, busy):
        rect = pygame.Rect(PANEL_X - 14, BOARD_Y - 14, PANEL_W + 28, 604)
        card(self.screen, rect, radius=18)

        assert self.game is not None
        over = self.game.is_over()

        y = BOARD_Y + 8
        if over:
            s1, s2 = self.game.score(0), self.game.score(1)
            verdict = "Player 1 wins" if s1 > s2 else "Player 2 wins" if s2 > s1 else "Draw"
            self.screen.blit(self.f_head.render(f"Game over - {verdict}", True, INK), (PANEL_X, y))
            y += 28
            self.screen.blit(self.f_body.render(f"{s1} - {s2}", True, MUTED), (PANEL_X, y))
            y += 40
        else:
            turn = self.game.turn
            self.screen.blit(self.f_head.render(f"{PLAYER_NAMES[turn]} to move", True, INK), (PANEL_X, y))
            y += 28
            note = f"Playing {COLOR_NAMES[self.game.player_color[turn]]}. Pick any unlocked color."
            self.screen.blit(self.f_body.render(note, True, MUTED), (PANEL_X, y))
            y += 40

        heading = self.f_head.render("Solver", True, INK)
        self.screen.blit(heading, (PANEL_X, y))
        if not over:
            note = self.f_badge.render(f"RANKED FOR P{self.game.turn + 1}", True, MUTED)
            self.screen.blit(note, note.get_rect(bottomright=(PANEL_X + PANEL_W, y + heading.get_height())))
        y += 32

        if error:
            for line in text_lines(self.f_small, error.replace("\n", " "), PANEL_W):
                self.screen.blit(self.f_small.render(line, True, (196, 64, 76)), (PANEL_X, y))
                y += 18
        elif over:
            self.screen.blit(self.f_body.render("Nothing left to search.", True, MUTED), (PANEL_X, y))
            y += 26
        elif result is None:
            dots = "." * (1 + int(time.time() * 3) % 3)
            label = "Compiling and searching" if busy else "Waiting"
            self.screen.blit(self.f_body.render(label + dots, True, MUTED), (PANEL_X, y))
            y += 26
        else:
            pid = self.game.turn
            best = result["best"]
            captures = result["captures"]

            # Colors that actually take tiles.  If there are none the player is
            # forced to pass, and then every move is on equal footing and every
            # score is meaningful.
            capturing = {c for c in result["scores"] if captures.get(c, 0)}

            def declines(colour_id: int) -> bool:
                return bool(capturing) and colour_id not in capturing

            # Strongest first, with the capture-less moves last.
            def rank(colour_id: int) -> tuple:
                margin = result["scores"][colour_id]
                if pid == 1:
                    margin = -margin
                return (declines(colour_id), -margin, colour_id != best, colour_id)

            for colour_id in sorted(result["scores"], key=rank):
                row = pygame.Rect(PANEL_X, y, PANEL_W, 40)
                top = colour_id == best

                pygame.draw.rect(self.screen, (243, 246, 252) if top else (248, 249, 250),
                                 row, border_radius=9)
                if top:
                    pygame.draw.rect(self.screen, ACCENT, row, width=2, border_radius=9)

                chip = pygame.Rect(row.left + 12, row.centery - 9, 18, 18)
                pygame.draw.rect(self.screen, PALETTE[colour_id], chip, border_radius=5)

                name = self.f_body.render(COLOR_NAMES[colour_id], True, INK if top else (92, 97, 106))
                self.screen.blit(name, name.get_rect(midleft=(chip.right + 10, row.centery)))

                if declines(colour_id):
                    # No comparable number exists for these.  Scoring one means
                    # playing it and then searching a game in which the
                    # opponent may not pass back, so what comes out is an upper
                    # bound rather than a value -- printing it would invite a
                    # comparison it cannot support.
                    note = self.f_badge.render("no capture", True, (176, 181, 190))
                    self.screen.blit(note, note.get_rect(midleft=(chip.right + 78, row.centery)))
                else:
                    margin = result["scores"][colour_id]
                    if pid == 1:
                        margin = -margin

                    if margin == 0:
                        verdict, tint = "draw", MUTED
                    elif margin > 0:
                        verdict, tint = f"wins by {margin}", (46, 140, 86)
                    else:
                        verdict, tint = f"loses by {-margin}", (196, 64, 76)

                    label = self.f_body.render(verdict, True, tint)
                    self.screen.blit(label, label.get_rect(midright=(row.right - 14, row.centery)))

                y += 46

            y += 6
            stats = f"{result['nodes']:,} nodes in {result['ms']} ms"
            self.screen.blit(self.f_small.render(stats, True, MUTED), (PANEL_X, y))
            y += 22

        self.btn_undo.enabled = bool(self.game.history)
        self.btn_undo.draw(self.screen, self.f_body)
        self.btn_edit.draw(self.screen, self.f_body)
        self.btn_new.draw(self.screen, self.f_body)

        hint = "1-6 play color    Space play best    U undo    E edit"
        self.screen.blit(self.f_small.render(hint, True, MUTED), (PANEL_X, rect.bottom - 34))


def main():
    App().run()


if __name__ == "__main__":
    main()
