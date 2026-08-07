# Filler

A two-player desktop version of **Filler**, the GamePigeon game, with a perfect-play solver
running alongside it. The board is an 8×7 grid of six colors. Player 1 starts from the
bottom-left tile, player 2 from the top-right. On your turn you pick a color — any color
except your own and your opponent's — and every tile of that color touching your territory
becomes yours. When the board is full, whoever holds more tiles wins.

The solver searches the game to the end with alpha-beta, so the move it shows is not a
heuristic guess: it is the move that wins if a win is available.

![The game mid-play](screenshot.png)

## Requirements

- **Python 3.10+** with **pygame** (`pip install pygame`)
- A **C++20** compiler on your `PATH` — `g++` or `clang++`
  - Windows: [MSYS2](https://www.msys2.org/) (`pacman -S mingw-w64-ucrt-x86_64-gcc`) or any MinGW-w64
  - Linux: `sudo apt install build-essential`
  - macOS: `xcode-select --install`

C++20 is required for `std::popcount` from `<bit>`.

## Running it

```bash
python filler.py
```

That's the whole install. `solver.cpp` is compiled automatically on first launch (a couple of
seconds, once) and rebuilt whenever you edit it, so there is no separate build step. The
compiled solver then runs as a single background process for the lifetime of the window.

## How to play

The app opens in the **board editor** on a random legal board.

| | |
|---|---|
| Click a color, then click or drag on the board | paint tiles |
| Right-click | erase a tile |
| `R` / `C` | randomize / clear the board |
| `1`–`6` | pick a paint color |
| `Enter` | start the game |

**Start** stays disabled until the board is legal, and the panel tells you what is wrong.
The only rule is that no two orthogonally adjacent tiles may share a color — see
[Editing rules](#editing-rules) for why that one matters.

Once you start, both players share the keyboard and mouse:

| | |
|---|---|
| Click an unlocked swatch, or `1`–`6` | play that color |
| Hover a swatch | outline exactly which tiles you would capture |
| `Space` | play the solver's recommendation |
| `U` | undo |
| `E` | back to the editor |
| `Esc` | quit |

The two locked swatches are the players' current colors. Each unlocked swatch shows `+N`
for how many tiles it would win you right now, and the solver's pick is ringed with **BEST**.
The panel gives the verdict for that move — *wins*, *loses*, or *draw* — with perfect play
from both sides. Territory is outlined rather than shaded, so you can still read the colors
underneath: player 1's border is solid dark, player 2's is white.

### Editing rules

A real Filler board never has two adjacent tiles of the same color, and the editor enforces
that. It is not cosmetic. Because neutral tiles never change color, that property means a
capture can never chain — the tiles you take are exactly the ones already touching your
territory. The solver relies on this and expands by a single step, so a board that breaks
the rule would get silently wrong answers.

Matching home corners **are** allowed. Whoever moves first simply leaves that color behind on
their opening turn, and they get five legal colors to choose from instead of four.

## How it works

The split is deliberate: Python owns the rules and the pixels, C++ owns the search.

- **`filler.py`** — the pygame front end. It enforces the game rules itself, so the GUI stays
  correct and playable even if the solver never starts. It compiles `solver.cpp` on demand,
  keeps one solver process alive, and queries it from a worker thread so the window never
  blocks on a search.
- **`solver.cpp`** — the engine. Bitboard state in a `uint64_t` per color, minimax with
  alpha-beta, a transposition table, and moves ordered by capture size. Wrapped in a
  request/response loop over stdin/stdout.
- **`main.cpp`** — the original standalone text version. Still works on its own; the GUI does
  not use it.

Openings usually resolve in well under a second.

### Solver protocol

One request per line group, one response per line group:

```
> SOLVE
> <56 chars>    tile colors, '0'..'5', index i = y*8 + x, y = 0 is the bottom row
> <56 chars>    owners, '.' neutral / '0' player 1 / '1' player 2
> <p1_color> <p2_color> <side_to_move>

< OK
< BEST <best_color> <color>:<score>,...
< NODES <n> <milliseconds>
< END
```

Scores are always from player 1's point of view: `1` means player 1 wins with perfect play,
`-1` player 2, `0` a draw. `best_color` is `-1` when the game is already over. `QUIT` ends the
process. Only the side to move is searched.

## Troubleshooting

**`no g++ or clang++ on PATH, and solver is not built`** — install a compiler (see above), or
build the solver by hand and drop it next to `filler.py`. On Windows it must be named
`solver.exe`:

```bash
g++ -O2 -std=c++20 -static -o solver.exe solver.cpp
```

On Linux and macOS it must be named `solver` (and drop `-static`, which macOS does not
support):

```bash
g++ -O2 -std=c++20 -o solver solver.cpp
```

**The compile fails with exit code 1 and no error message** — on Windows this usually means a
second MinGW installation earlier on your `PATH` (Git for Windows ships one) is shadowing the
runtime DLLs your compiler needs. `filler.py` already works around this by putting the
compiler's own directory first, so if you hit it while building by hand, do the same.

**`No module named 'pygame'`** — you have more than one Python and `python` is resolving to
the wrong one. Check with `python -c "import pygame"`. On Windows, the `py` launcher usually
picks the right interpreter: `py filler.py`. This bites in particular if MSYS2 is on your
`PATH`, since it ships its own `python` that will not see packages you installed elsewhere.

## Credits

- **[mattbatwings](https://youtube.com/mattbatwings)** — the game logic and the solver: the
  bitboard representation, expansion, and the minimax search that everything else is built
  around.
- **Claude** (Anthropic's Claude Code) — the pygame GUI, the board editor, and the
  request/response wrapper that lets Python talk to the engine.
