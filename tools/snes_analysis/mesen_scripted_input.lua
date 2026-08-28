-- mesen_scripted_input.lua — drive the reference with a button script, then
-- dump PPU state and a screenshot at chosen frames.
--
-- The oracle side of any defect that lives behind a menu. snesrecomp's host can
-- be driven headlessly with SNESRECOMP_INPUT_SCRIPT; without the same ability
-- here there is nothing to compare a menu screen against, and "what should this
-- screen look like" stays an opinion.
--
-- Run:
--     mesen-ce <rom> mesen_scripted_input.lua
--
-- Environment:
--     GW_SCRIPT   "frame:buttons,frame:buttons,..." — buttons are letters from
--                 a b x y l r s(elect) t(=start) u d n(left) m(right).
--                 The state holds until the next entry. e.g. "400:t,430:,520:t,550:"
--     GW_DUMP     comma-separated frames to dump at (default 640,660,700)
--     GW_DIR      output directory (default /tmp/mesen_scripted)
--     GW_UNTIL    stop after this frame (default 4000)
--
-- Requires Debug > ScriptWindow > AllowIoOsAccess = true, set while Mesen is
-- NOT running (it rewrites settings.json on exit).

local DIR = os.getenv("GW_DIR") or "/tmp/mesen_scripted"
local UNTIL = tonumber(os.getenv("GW_UNTIL") or "") or 4000
local SPEC = os.getenv("GW_SCRIPT") or ""
local DUMPS = os.getenv("GW_DUMP") or "640,660,700"

os.execute('mkdir -p "' .. DIR .. '"')

local steps = {}
for frame, btns in string.gmatch(SPEC, "(%d+):([^,]*)") do
  steps[#steps + 1] = { frame = tonumber(frame), btns = btns }
end
table.sort(steps, function(a, b) return a.frame < b.frame end)

local dumpAt = {}
for f in string.gmatch(DUMPS, "%d+") do dumpAt[tonumber(f)] = true end

local LETTER = { a="a", b="b", x="x", y="y", l="l", r="r",
                 s="select", t="start", u="up", d="down", n="left", m="right" }

local frame = 0

local function inputFor(spec)
  local st = {}
  for c in string.gmatch(spec, ".") do
    local k = LETTER[c]
    if k then st[k] = true end
  end
  return st
end

local function currentSpec()
  local cur = ""
  for _, s in ipairs(steps) do
    if frame >= s.frame then cur = s.btns end
  end
  return cur
end

-- Input must be applied every frame: Mesen's setInput is a one-frame override,
-- not a latched state.
emu.addEventCallback(function()
  local ok, err = pcall(function()
    emu.setInput(0, inputFor(currentSpec()))
  end)
  if not ok and frame == 1 then emu.log("setInput failed: " .. tostring(err)) end
end, emu.eventType.inputPolled)

local function dump(tag)
  local st = emu.getState()
  local keys = {
    "ppu.bgMode", "ppu.mainScreenLayers", "ppu.subScreenLayers",
    "ppu.forcedBlank", "ppu.screenBrightness",
    "ppu.colorMathEnabled", "ppu.colorMathAddSubscreen",
    "ppu.colorMathHalveResult", "ppu.colorMathSubtractMode", "ppu.fixedColor",
    "ppu.window[0].left", "ppu.window[0].right",
    "ppu.window[1].left", "ppu.window[1].right",
    "ppu.layers[0].tilemapAddress", "ppu.layers[0].chrAddress",
    "ppu.layers[1].tilemapAddress", "ppu.layers[1].chrAddress",
    "ppu.layers[2].tilemapAddress", "ppu.layers[2].chrAddress",
    "ppu.layers[3].tilemapAddress", "ppu.layers[3].chrAddress",
  }
  local f = io.open(string.format("%s/%s_state.txt", DIR, tag), "w")
  if f then
    f:write("frame=" .. frame .. "\n")
    for _, k in ipairs(keys) do f:write(k .. "=" .. tostring(st[k]) .. "\n") end
    -- CGRAM census: the same "is the palette stale" question the runtime asks.
    local counts, distinct = {}, 0
    for i = 0, 255 do
      local lo = emu.read(i * 2, emu.memType.snesCgRam)
      local hi = emu.read(i * 2 + 1, emu.memType.snesCgRam)
      local v = lo + hi * 256
      if not counts[v] then counts[v] = 0; distinct = distinct + 1 end
      counts[v] = counts[v] + 1
    end
    local topv, topn = 0, 0
    for v, n in pairs(counts) do if n > topn then topv, topn = v, n end end
    f:write(string.format("cgram.distinct=%d\ncgram.top=0x%04X\ncgram.topCount=%d\n",
                          distinct, topv, topn))
    f:close()
  end
  -- Full CGRAM, not just the census. A palette that ANIMATES per frame is
  -- invisible in "distinct/top/topCount" -- those can be identical while
  -- every entry has shifted one step along a glow ramp, which is exactly the
  -- failure this was added to catch. The recomp side reads the same bytes
  -- from get_frame_extended, so the two are directly comparable.
  local cg = {}
  for i = 0, 511 do cg[#cg + 1] = string.char(emu.read(i, emu.memType.snesCgRam) % 256) end
  local cf = io.open(string.format("%s/%s_cgram.bin", DIR, tag), "wb")
  if cf then cf:write(table.concat(cg)); cf:close() end

  -- OAM as well, so each signal's frame alignment can be measured SEPARATELY.
  -- A uniform one-frame shift of everything is invisible on screen; a shift of
  -- the palette RELATIVE to the sprites is the visible defect. Only per-signal
  -- offsets can tell those apart.
  local oa = {}
  for i = 0, 543 do oa[#oa + 1] = string.char(emu.read(i, emu.memType.snesSpriteRam) % 256) end
  local af = io.open(string.format("%s/%s_oam.bin", DIR, tag), "wb")
  if af then af:write(table.concat(oa)); af:close() end

  -- Low WRAM ($7E:0000-1FFF), where the game's per-frame variables live.
  -- When the two emulators disagree about what is ON SCREEN, this is the
  -- fork: if guest WRAM matches, the divergence is downstream in the PPU/DMA
  -- path; if it does not, the guest is executing differently and nothing in
  -- the renderer will explain it. Only the low 8K -- 128K of emu.read per
  -- frame is far too slow to run every frame.
  local wr = {}
  for i = 0, 0x1FFF do wr[#wr + 1] = string.char(emu.read(i, emu.memType.snesWorkRam) % 256) end
  local wf = io.open(string.format("%s/%s_wram.bin", DIR, tag), "wb")
  if wf then wf:write(table.concat(wr)); wf:close() end

  local png = emu.takeScreenshot()
  if png then
    local g = io.open(string.format("%s/%s.png", DIR, tag), "wb")
    if g then g:write(png); g:close() end
  end
  emu.log("dumped " .. tag .. " at frame " .. frame)
end

-- Per-frame trace of the handful of variables that decide what the game does
-- each frame, written every frame rather than only at dump points. Finding
-- the FIRST frame where the two runs disagree needs a continuous timeline;
-- sampling only at the frames you already suspect cannot find it.
--   $0A = NMI counter, $0E = vblank flag, $10 = NMI dispatch index
emu.addEventCallback(function()
  frame = frame + 1
  if dumpAt[frame] then dump(string.format("f%06d", frame)) end
  if (frame % 300) == 0 then
    local hb = io.open(DIR .. "/heartbeat.txt", "w")
    if hb then hb:write("frame=" .. frame .. "\n"); hb:close() end
  end
end, emu.eventType.endFrame)

local m = io.open(DIR .. "/loaded.txt", "w")
if m then m:write(string.format("loaded, %d step(s), dumps at %s\n", #steps, DUMPS)); m:close() end
emu.log("mesen_scripted_input: " .. #steps .. " step(s) -> " .. DIR)
