# Driving Studio to check what it actually renders

For three sessions running, GUI changes here shipped with "builds clean, tests
pass, **not visually verified**" attached — because nothing could click a tab.
That gap is not academic. The SNES Functions tab passed its data tests, passed
a wrapping lint, and compiled without a warning while its filter row drew the
Find box on top of the Show dropdown. One screenshot found it in seconds.

`PRINCIPLES.md` already says this: *screenshot first, claim second*, and *"the
screenshot command returns black" is not a reason to skip visual verification.
It is a reason to fix the screenshot command.* This is the fixed command.

## The tools

- **xdotool** — built from source into `~/.local` (which is on PATH), because
  the distro package needs root and this does not:

  ```sh
  git clone --depth 1 https://github.com/jordansissel/xdotool.git
  cd xdotool && make PREFIX="$HOME/.local" -j"$(nproc)"
  make install PREFIX="$HOME/.local" INSTALLMAN="$HOME/.local/share/man"
  ```

  The `ldconfig … Permission denied` at the end is expected and harmless — the
  binary carries an rpath to `~/.local/lib`, so it resolves `libxdo` without a
  system cache entry.

- **wmctrl** and ImageMagick's **import** were already installed.
- **python-xlib 0.33** is also present, if you ever want XTEST synthesis from
  Python instead.

## The recipe

```sh
./build/Retro-Studio &                       # your own instance, not the user's
sleep 5
WID=$(xdotool search --name "^Retro Studio$" | tail -1)
xdotool windowmove "$WID" 0 60
xdotool windowsize "$WID" 924 900
wmctrl -i -a "$WID"                            # see "activation" below
sleep 2

# click a tab: hover has to exist for a frame or two before the press
xdotool mousemove --window "$WID" 400 300 ; sleep 1
xdotool mousemove --window "$WID" 542 172 ; sleep 1
xdotool click 1

sleep 3
import -window "$WID" /tmp/shot.png
```

## Four things that cost time, in the order they bite

**Activation.** `xdotool windowactivate --sync` is not always enough to get
Studio above another top-level window; `wmctrl -i -a "$WID"` uses a different
activation path and worked every time it was tried. If a capture comes back
black or shows someone else's window, this is why.

**A capture of an occluded GL window is black.** `import -window` reads the
screen region, not a redirected backing pixmap, so an SDL/OpenGL window that is
covered captures as a black rectangle — or worse, captures whatever is on top
of it and looks like a real screenshot of the wrong program. Always confirm
`xdotool getactivewindow` matches before believing an image.

**The desktop rect lies.** KDE reports `Desktop @ QRect(0,0 3840x2160)` while
the composited area is 1920 wide. Moving a window to x=1960 renders nothing and
captures pure black. Keep windows inside 0..1920.

**ImGui needs hover before the press.** `xdotool mousemove … click 1` in one
invocation can deliver motion and button-press inside a single frame, and the
tab highlights but does not switch. Move, sleep, move again, sleep, then click.

## Do not fight another session for the screen

Check `pgrep -af` for other Claude sessions before raising windows. When one is
found, check *how* it captures before assuming a conflict: the session running
concurrently with this write-up drove Mesen through `emu.*` Lua and the runtime
through the debug protocol's `screenshot` command — neither touches X11, so
raising an unrelated window could not corrupt its evidence. That is the
doctrine's own rule (*capture through the debug surface, never by foregrounding*)
paying off in an unexpected direction.

Verify it rather than assume it, and move your own window to free space rather
than lowering theirs.
