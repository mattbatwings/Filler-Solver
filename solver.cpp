// Filler solver -- engine core lifted from main.cpp, wrapped in a persistent
// request/response loop so a GUI can keep one process alive and query it.
//
// Protocol (line based, on stdin/stdout):
//
//   > SOLVE
//   > <56 chars>          colors,  '0'..'5',   index i = y*WIDTH + x, y=0 is the bottom row
//   > <56 chars>          owners,  '.' neutral / '0' player 1 / '1' player 2
//   > <p1_color> <p2_color> <side_to_move>      side 0 = player 1, 1 = player 2
//
//   < OK
//   < BEST <best_color> <color>:<score>,...
//   < NODES <n> <milliseconds>
//   < END
//
// Only the side to move is searched.  Scores are always from player 1's point
// of view: 1 = player 1 wins with perfect play, -1 = player 2 wins, 0 = draw.
// best_color is -1 when the game is already over.  Any malformed request
// answers with a single "ERR <reason>".
//
//   > QUIT      terminates.

#include <algorithm>
#include <array>
#include <bit>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

using namespace std;

constexpr int WIDTH = 8;
constexpr int HEIGHT = 7;
constexpr int NCELLS = WIDTH * HEIGHT;

using Bitboard = uint64_t;

struct State {
    array<Bitboard, 6> colors;
    array<Bitboard, 2> players;
    int p1_color;
    int p2_color;
};

constexpr Bitboard MASK = (1ULL << NCELLS) - 1;

Bitboard LEFT;
Bitboard RIGHT;

void initialize_masks() {
    Bitboard left_col = 0;
    Bitboard right_col = 0;

    for (int y = 0; y < HEIGHT; y++) {
        left_col |= 1ULL << (y * WIDTH);
        right_col |= 1ULL << (y * WIDTH + WIDTH - 1);
    }

    LEFT = MASK ^ left_col;
    RIGHT = MASK ^ right_col;
}

// One expansion ring.  A legal board never has two orthogonally adjacent cells
// of the same color, and neutral cells never change color, so a single ring is
// the complete capture -- same-colored cells can never chain together.
Bitboard expand_into(Bitboard A, Bitboard B) {
    Bitboard neighbors =
        ((A << WIDTH) |
         (A >> WIDTH) |
         ((A & RIGHT) << 1) |
         ((A & LEFT) >> 1))
        & MASK;

    return A | (B & neighbors);
}

int popcount(Bitboard x) {
    return std::popcount(x);
}

State take_turn(const State &s, int new_color, Bitboard expanded, bool p1_turn) {
    State ns = s;

    int old_color = (p1_turn) ? s.p1_color : s.p2_color;
    int player_idx = (p1_turn) ? 0 : 1;

    ns.colors[old_color] &= ~s.players[player_idx];
    ns.colors[new_color] |= s.players[player_idx];
    ns.players[player_idx] = expanded;

    if (p1_turn)
        ns.p1_color = new_color;
    else
        ns.p2_color = new_color;

    return ns;
}

bool game_over(const State &s) {
    return (s.players[0] | s.players[1]) == MASK;
}

struct Move {
    int color;
    Bitboard expanded;
    int gain;
};

// Legal moves for a player, best-gain first.  When nothing expands the player
// must still recolor (which changes what the opponent may pick), so every legal
// color is returned as a zero-gain move.
int generate_moves(const State &s, int player_idx, Move *out) {
    int n = 0;
    int owned = popcount(s.players[player_idx]);

    for (int color = 0; color < 6; color++) {
        if (color == s.p1_color || color == s.p2_color)
            continue;

        Bitboard expanded = expand_into(s.players[player_idx], s.colors[color]);
        if (expanded == s.players[player_idx])
            continue;

        out[n++] = {color, expanded, popcount(expanded) - owned};
    }

    if (n == 0) {
        for (int color = 0; color < 6; color++) {
            if (color == s.p1_color || color == s.p2_color)
                continue;

            out[n++] = {color, s.players[player_idx], 0};
        }
    } else {
        sort(out, out + n, [](const Move &a, const Move &b) { return a.gain > b.gain; });
    }

    return n;
}

// --- transposition table ------------------------------------------------
//
// The neutral part of the board never changes, so within one request a
// position is fully described by the two territory masks plus both colors.

constexpr int TT_BITS = 21;
constexpr size_t TT_SIZE = size_t(1) << TT_BITS;

enum : int8_t { TT_EXACT = 0, TT_LOWER = 1, TT_UPPER = 2 };

struct TTEntry {
    uint64_t key;
    int8_t value;
    int8_t flag;
    uint16_t epoch;
};

static vector<TTEntry> tt(TT_SIZE);
static uint16_t tt_epoch = 0;
static uint64_t nodes = 0;

static inline uint64_t splitmix64(uint64_t x) {
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

static inline uint64_t state_key(const State &s, bool maximizing) {
    uint64_t k = splitmix64(s.players[0]);
    k ^= splitmix64(s.players[1] + 0x9E3779B97F4A7C15ULL);
    k ^= splitmix64((uint64_t(s.p1_color) << 4) | (uint64_t(s.p2_color) << 1) |
                    uint64_t(maximizing));
    return k;
}

int minimax(const State &s, int alpha, int beta, bool maximizing) {
    nodes++;

    if (game_over(s)) {
        int p1 = popcount(s.players[0]);
        int p2 = popcount(s.players[1]);

        if (p1 > p2)
            return 1;
        else if (p1 < p2)
            return -1;
        else
            return 0;
    }

    uint64_t key = state_key(s, maximizing);
    TTEntry &slot = tt[key & (TT_SIZE - 1)];

    if (slot.epoch == tt_epoch && slot.key == key) {
        if (slot.flag == TT_EXACT)
            return slot.value;
        if (slot.flag == TT_LOWER && slot.value >= beta)
            return slot.value;
        if (slot.flag == TT_UPPER && slot.value <= alpha)
            return slot.value;
    }

    int alpha0 = alpha;
    int beta0 = beta;

    int player_idx = maximizing ? 0 : 1;
    int bestEval = maximizing ? -1 : 1;

    Move moves[6];
    int n = generate_moves(s, player_idx, moves);

    for (int i = 0; i < n; i++) {
        State ns = take_turn(s, moves[i].color, moves[i].expanded, maximizing);
        int eval = minimax(ns, alpha, beta, !maximizing);

        if (maximizing) {
            bestEval = max(bestEval, eval);
            alpha = max(alpha, bestEval);
        } else {
            bestEval = min(bestEval, eval);
            beta = min(beta, bestEval);
        }

        if (alpha >= beta)
            break;
    }

    slot.key = key;
    slot.value = int8_t(bestEval);
    slot.epoch = tt_epoch;
    slot.flag = (bestEval <= alpha0) ? TT_UPPER : (bestEval >= beta0) ? TT_LOWER : TT_EXACT;

    return bestEval;
}

struct Analysis {
    int best_color = -1;
    array<int, 6> score{};   // score per color, from player 1's point of view
    array<bool, 6> legal{};  // which colors were actually searched
};

Analysis best_move(const State &s, bool maximizing) {
    Analysis a;
    a.score.fill(0);
    a.legal.fill(false);

    if (game_over(s))
        return a;

    int player_idx = maximizing ? 0 : 1;
    int best_score = maximizing ? -1 : 1;

    Move moves[6];
    int n = generate_moves(s, player_idx, moves);

    for (int i = 0; i < n; i++) {
        State ns = take_turn(s, moves[i].color, moves[i].expanded, maximizing);
        int score = minimax(ns, -1, 1, !maximizing);

        a.legal[moves[i].color] = true;
        a.score[moves[i].color] = score;

        if (a.best_color == -1 || (maximizing ? score > best_score : score < best_score)) {
            best_score = score;
            a.best_color = moves[i].color;
        }
    }

    return a;
}

// --- request handling ---------------------------------------------------

static bool build_state(const string &color_line, const string &owner_line,
                        int p1_color, int p2_color, State &out, string &err) {
    if ((int)color_line.size() != NCELLS) {
        err = "color line must be " + to_string(NCELLS) + " chars";
        return false;
    }

    if ((int)owner_line.size() != NCELLS) {
        err = "owner line must be " + to_string(NCELLS) + " chars";
        return false;
    }

    if (p1_color < 0 || p1_color > 5 || p2_color < 0 || p2_color > 5) {
        err = "player colors out of range";
        return false;
    }

    out.colors.fill(0);
    out.players.fill(0);
    out.p1_color = p1_color;
    out.p2_color = p2_color;

    for (int i = 0; i < NCELLS; i++) {
        int c = color_line[i] - '0';
        if (c < 0 || c > 5) {
            err = "bad color char at " + to_string(i);
            return false;
        }

        out.colors[c] |= 1ULL << i;

        char o = owner_line[i];
        if (o == '0')
            out.players[0] |= 1ULL << i;
        else if (o == '1')
            out.players[1] |= 1ULL << i;
        else if (o != '.') {
            err = "bad owner char at " + to_string(i);
            return false;
        }
    }

    return true;
}

static void print_analysis(const char *tag, const Analysis &a) {
    cout << tag << ' ' << a.best_color << ' ';

    bool first = true;
    for (int c = 0; c < 6; c++) {
        if (!a.legal[c])
            continue;

        if (!first)
            cout << ',';

        cout << c << ':' << a.score[c];
        first = false;
    }

    if (first)
        cout << '-';

    cout << '\n';
}

int main() {
    ios::sync_with_stdio(false);
    initialize_masks();

    string line;

    while (getline(cin, line)) {
        while (!line.empty() && (line.back() == '\r' || line.back() == ' '))
            line.pop_back();

        if (line == "QUIT" || line == "EXIT")
            break;

        if (line == "PING") {
            cout << "PONG\n" << flush;
            continue;
        }

        if (line != "SOLVE") {
            if (line.empty())
                continue;

            cout << "ERR unknown command\n" << flush;
            continue;
        }

        string color_line;
        string owner_line;
        string tail_line;

        if (!getline(cin, color_line) || !getline(cin, owner_line) ||
            !getline(cin, tail_line)) {
            break;
        }

        auto trim = [](string &s) {
            while (!s.empty() && (s.back() == '\r' || s.back() == ' ' || s.back() == '\n'))
                s.pop_back();
        };

        trim(color_line);
        trim(owner_line);
        trim(tail_line);

        int p1_color = -1;
        int p2_color = -1;
        int side = 0;
        {
            istringstream in(tail_line);
            in >> p1_color >> p2_color >> side;
        }

        State s;
        string err;

        if (!build_state(color_line, owner_line, p1_color, p2_color, s, err)) {
            cout << "ERR " << err << '\n' << flush;
            continue;
        }

        if (side != 0 && side != 1) {
            cout << "ERR side to move must be 0 or 1\n" << flush;
            continue;
        }

        tt_epoch++;
        if (tt_epoch == 0) {   // wrapped -- stale entries could alias, so wipe
            fill(tt.begin(), tt.end(), TTEntry{});
            tt_epoch = 1;
        }

        nodes = 0;
        auto start = chrono::steady_clock::now();

        Analysis best = best_move(s, side == 0);

        auto elapsed = chrono::duration_cast<chrono::milliseconds>(
                           chrono::steady_clock::now() - start)
                           .count();

        cout << "OK\n";
        print_analysis("BEST", best);
        cout << "NODES " << nodes << ' ' << elapsed << '\n';
        cout << "END\n" << flush;
    }

    return 0;
}
