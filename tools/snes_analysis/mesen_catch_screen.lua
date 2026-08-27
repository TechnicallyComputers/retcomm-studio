-- mesen_catch_screen.lua — the oracle twin of snes_catch_screen.py.
--
-- Wait until the reference is showing a screen dominated by a given colour,
-- then dump its PPU state and a screenshot. Needed because the two emulators
-- do not follow the same path from the same button script — frame numbers and
-- menu timing drift — so "dump at frame N" compares two different screens and
-- quietly invents a divergence. Matching on what is ON SCREEN is the only
-- alignment that holds.
--
-- Run:
--     mesen-ce <rom> mesen_catch_screen.lua
--
-- Environment:
--     GW_SCRIPT   button script, "frame:letters,..." (see mesen_scripted_input.lua)
--     GW_RGB      target colour, hex RRGGBB (default 083900)
--     GW_TOL      per-channel tolerance (default 40)
--     GW_SHARE    minimum share of the screen, 0..1 (default 0.5)
--     GW_DIR      output directory (default /tmp/mesen_catch)
--     GW_UNTIL    give up after this frame (default 6000)
--
-- Requires Debug > ScriptWindow > AllowIoOsAccess = true, set while stopped.

local DIR   = os.getenv("GW_DIR") or "/tmp/mesen_catch"
local RGB   = os.getenv("GW_RGB") or "083900"
local TOL   = tonumber(os.getenv("GW_TOL") or "") or 40
local SHARE = tonumber(os.getenv("GW_SHARE") or "") or 0.5
local UNTIL = tonumber(os.getenv("GW_UNTIL") or "") or 6000
local SPEC  = os.getenv("GW_SCRIPT") or ""

os.execute('mkdir -p "' .. DIR .. '"')

local WANT_R = tonumber(RGB:sub(1, 2), 16)
local WANT_G = tonumber(RGB:sub(3, 4), 16)
local WANT_B = tonumber(RGB:sub(5, 6), 16)

local steps = {}
for f, b in string.gmatch(SPEC, "(%d+):([^,]*)") do
  steps[#steps + 1] = { frame = tonumber(f), btns = b }
end
table.sort(steps, function(a, b) return a.frame < b.frame end)

local LETTER = { a="a", b="b", x="x", y="y", l="l", r="r",
                 s="select", t="start", u="up", d="down", n="left", m="right" }
local frame, done = 0, false

emu.addEventCallback(function()
  local cur = ""
  for _, s in ipairs(steps) do if frame >= s.frame then cur = s.btns end end
  local st = {}
  for c in string.gmatch(cur, ".") do
    local k = LETTER[c]; if k then st[k] = true end
  end
  pcall(function() emu.setInput(0, st) end)
end, emu.eventType.inputPolled)

local function dump()
  local s = emu.getState()
  local f = io.open(DIR .. "/catch_state.txt", "w")
  if f then
    f:write("frame=" .. frame .. "\n")
    for _, k in ipairs({
      "ppu.bgMode", "ppu.mainScreenLayers", "ppu.subScreenLayers",
      "ppu.forcedBlank", "ppu.screenBrightness", "ppu.mode1Bg3Priority",
      "ppu.colorMathEnabled", "ppu.colorMathAddSubscreen",
      "ppu.colorMathHalveResult", "ppu.colorMathSubtractMode", "ppu.fixedColor",
      "ppu.mainScreenWindowMask", "ppu.subScreenWindowMask",
      "ppu.window[0].left", "ppu.window[0].right",
      "ppu.window[1].left", "ppu.window[1].right",
      "ppu.layers[0].tilemapAddress", "ppu.layers[0].chrAddress",
      "ppu.layers[0].largeTiles", "ppu.layers[0].hscroll", "ppu.layers[0].vscroll",
      "ppu.layers[1].tilemapAddress", "ppu.layers[1].chrAddress",
      "ppu.layers[1].largeTiles", "ppu.layers[1].hscroll", "ppu.layers[1].vscroll",
      "ppu.layers[2].tilemapAddress", "ppu.layers[2].chrAddress",
      "ppu.layers[2].hscroll", "ppu.layers[2].vscroll",
      "ppu.layers[3].tilemapAddress", "ppu.layers[3].chrAddress",
      "ppu.oamBaseAddress", "ppu.oamMode",
    }) do f:write(k .. "=" .. tostring(s[k]) .. "\n") end
    local counts, distinct = {}, 0
    for i = 0, 255 do
      local v = emu.read(i * 2, emu.memType.snesCgRam)
            + emu.read(i * 2 + 1, emu.memType.snesCgRam) * 256
      if not counts[v] then counts[v] = 0; distinct = distinct + 1 end
      counts[v] = counts[v] + 1
    end
    local tv, tn = 0, 0
    for v, n in pairs(counts) do if n > tn then tv, tn = v, n end end
    f:write(string.format("cgram.distinct=%d\ncgram.top=0x%04X\ncgram.topCount=%d\n",
                          distinct, tv, tn))
    f:close()
  end
  local png = emu.takeScreenshot()
  if png then
    local g = io.open(DIR .. "/catch.png", "wb")
    if g then g:write(png); g:close() end
  end
  emu.log("caught at frame " .. frame)
end

emu.addEventCallback(function()
  frame = frame + 1
  if done or frame > UNTIL then return end
  if (frame % 4) ~= 0 then return end          -- sampling every 4th frame is plenty
  local ok, buf = pcall(emu.getScreenBuffer)
  if not ok or not buf then return end
  local hit, total = 0, #buf
  for i = 1, total, 7 do                        -- stride-sample; exactness not needed
    local p = buf[i]
    local r = (p >> 16) & 0xFF
    local g = (p >> 8) & 0xFF
    local b = p & 0xFF
    if math.abs(r - WANT_R) <= TOL and math.abs(g - WANT_G) <= TOL
       and math.abs(b - WANT_B) <= TOL then hit = hit + 1 end
  end
  local sampled = math.floor(total / 7) + 1
  if hit / sampled >= SHARE then
    done = true
    dump()
  end
  if (frame % 400) == 0 then
    local h = io.open(DIR .. "/heartbeat.txt", "w")
    if h then h:write(string.format("frame=%d share=%.2f\n", frame, hit / sampled)); h:close() end
  end
end, emu.eventType.endFrame)

local m = io.open(DIR .. "/loaded.txt", "w")
if m then m:write(string.format("watching for #%s +/-%d share>=%.2f\n", RGB, TOL, SHARE)); m:close() end
emu.log("mesen_catch_screen: watching for #" .. RGB)
