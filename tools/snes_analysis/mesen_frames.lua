-- mesen_frames.lua — dump a RUN of consecutive frames as PNG, nothing else.
--
-- The sibling dump scripts write WRAM/CGRAM/OAM/state alongside each frame,
-- which is far too slow to capture a whole animation cycle. Comparing only
-- part of a cycle is how a defect gets missed: a 14-frame sample of a
-- 128-frame loop covers 11% of the phases, and a defect that sits at ONE
-- phase is invisible both to that sample and to a period test (which sees a
-- consistently-wrong frame as consistent).
--
-- Run:
--     GW_FROM=3500 GW_COUNT=160 GW_DIR=... GW_SCRIPT=... mesen-ce <rom> this.lua

-- GW_WATCH lets the capture start on GUEST STATE rather than a frame number.
-- Mesen's Lua frame counter starts at script load, and that lag varies by
-- hundreds of frames between launches -- targeting a window by number misses
-- the scene as often as it hits it. Format "ADDR=VALUE" in hex, e.g.
-- GW_WATCH=0010=02 starts the capture on the first frame where WRAM $7E:0010
-- reads 0x02, and GW_FROM is then ignored.
local DIR   = os.getenv("GW_DIR") or "/tmp/mesen_frames"
local FROM  = tonumber(os.getenv("GW_FROM") or "") or 3500
local COUNT = tonumber(os.getenv("GW_COUNT") or "") or 160
local SPEC  = os.getenv("GW_SCRIPT") or ""
-- OAM costs 544 emu.read per frame; opt in, so a pure picture
-- capture stays fast enough to cover a whole animation cycle.
local GW_OAM = (os.getenv("GW_OAM") or "") ~= ""
-- VRAM is 64K of emu.read per frame and is SLOW — seconds per frame, not
-- milliseconds. It exists to answer "does hardware write these tile words on
-- the same frame it updates OAM"; a dozen frames answers that, so pair it
-- with a small GW_COUNT. Region is bounded by GW_VRAM_LO/GW_VRAM_HI (byte
-- addresses, hex) to keep it tolerable; default covers the OBJ character
-- data this title streams.
local GW_VRAM = (os.getenv("GW_VRAM") or "") ~= ""
-- CGRAM is only 512 bytes, but capture it explicitly rather than assuming the
-- palette is static: this title rewrites 35-46 CGRAM bytes on every sprite-table
-- update, so the palette alternates WITH the animation phase. Feeding an oracle
-- VRAM+OAM capture into another emulator's renderer while that emulator supplies
-- its own live CGRAM produces a hybrid that proves nothing.
local GW_CGRAM = (os.getenv("GW_CGRAM") or "") ~= ""
-- Low WRAM ($7E:0000-$1FFF) carries this title's frame counter (DP $08), the
-- per-object Y offsets ($1181+X) and the shadow OAM ($0D00). Capturing it is
-- how you ask "does hardware apply the sprite-animation update and the
-- 1-pixel hover in the SAME frame, or in different ones" — which OAM alone
-- cannot answer, because OAM only shows the published result.
local GW_WRAM = (os.getenv("GW_WRAM") or "") ~= ""
local VLO = tonumber(os.getenv("GW_VRAM_LO") or "8000", 16) or 0x8000
local VHI = tonumber(os.getenv("GW_VRAM_HI") or "AFFF", 16) or 0xAFFF

os.execute('mkdir -p "' .. DIR .. '"')

-- Clear stale frames on the FIRST WRITE, not at script load.
--
-- Mesen's Lua frame counter restarts at script load, so a second run into the
-- same directory writes a DIFFERENT frame range and leaves the first run's
-- files beside it; three stacked runs were once mistaken for one 600-frame
-- capture, with the stale frames from a different matchup entirely. But
-- clearing at LOAD destroys a good capture whenever a later run is armed and
-- then never triggers -- which is exactly how a 300-frame capture was lost.
-- Clearing when the first frame is actually written keeps both properties:
-- no mixing, and an un-triggered run costs nothing.
local cleared = false
local function clear_once()
  if cleared then return end
  cleared = true
  os.execute('rm -f "' .. DIR .. '"/f*.png "' .. DIR .. '"/f*_oam.bin '
             .. '"' .. DIR .. '"/f*_vram.bin "' .. DIR .. '"/f*_cgram.bin '
             .. '"' .. DIR .. '"/f*_wram.bin')
end

local steps = {}
for f, b in string.gmatch(SPEC, "(%d+):([^,]*)") do
  steps[#steps + 1] = { frame = tonumber(f), btns = b }
end
table.sort(steps, function(a, b) return a.frame < b.frame end)

local LETTER = { a="a", b="b", x="x", y="y", l="l", r="r",
                 s="select", t="start", u="up", d="down", n="left", m="right" }
local frame = 0

-- Only take over the pad when a script was actually given. Registering this
-- with an empty GW_SCRIPT calls setInput({}) every frame, which silently
-- clears the real controller and makes the emulator undriveable -- so a
-- human cannot steer it to the screen the capture is waiting for.
if #steps > 0 then
  emu.addEventCallback(function()
    local cur = ""
    for _, s in ipairs(steps) do if frame >= s.frame then cur = s.btns end end
    local st = {}
    for c in string.gmatch(cur, ".") do
      local k = LETTER[c]; if k then st[k] = true end
    end
    pcall(function() emu.setInput(0, st) end)
  end, emu.eventType.inputPolled)
end

-- GW_MINSPR=<n>: start only once at least n sprites are actually ON SCREEN
-- (OAM Y < 0xE0). A WRAM marker is a poor scene gate on this title -- $0012
-- reads 0x14 at boot AND during the fade into the pre-fight screen, so a
-- GW_WATCH capture landed 16 frames of empty starfield with zero sprites.
-- Sprite count cannot fire early: the mechs are the thing being measured.
local MINSPR = tonumber(os.getenv("GW_MINSPR") or "") or 0
-- Count visible sprites AND how many distinct tiles they use. The count alone
-- is not enough: at power-on OAM is all zeros, Y=0 reads as "visible", so all
-- 128 slots pass and the capture fires on frame 1. Real content uses many
-- distinct tile indices; a cleared table uses one.
local function sprite_state()
  local n, tiles, ntiles = 0, {}, 0
  for i = 0, 127 do
    if emu.read(i * 4 + 1, emu.memType.snesSpriteRam) < 0xE0 then
      n = n + 1
      local t = emu.read(i * 4 + 2, emu.memType.snesSpriteRam)
      if not tiles[t] then tiles[t] = true; ntiles = ntiles + 1 end
    end
  end
  return n, ntiles
end

-- GW_GO=1: start when the file <DIR>/GO appears.
--
-- Every automatic gate tried on this title fired on the wrong screen: the
-- WRAM marker $0012=0x14 reads 0x14 at boot and during the fade-in, and a
-- sprite-count gate fires on the title/attract screens, which also have plenty
-- of varied sprites. When a human is driving the emulator anyway, the human
-- knows when the screen is right -- so let them say so. One io.open per frame
-- is nothing next to a screenshot.
local GW_GO = (os.getenv("GW_GO") or "") ~= ""
local function go_present()
  local f = io.open(DIR .. "/GO", "r")
  if f then f:close(); return true end
  return false
end

local WATCH = os.getenv("GW_WATCH")
local wAddr, wVal
if WATCH then
  local a, v = string.match(WATCH, "(%x+)=(%x+)")
  if a then wAddr = tonumber(a, 16); wVal = tonumber(v, 16) end
end
local started = nil

emu.addEventCallback(function()
  frame = frame + 1
  if (wAddr or MINSPR > 0 or GW_GO) and not started then
    local ok, cur = true, nil
    if wAddr then
      ok, cur = pcall(function()
        return emu.read(wAddr, emu.memType.snesWorkRam)
      end)
    end
    local marker_ok = (not wAddr) or (ok and cur == wVal)
    local spr_ok = true
    if MINSPR > 0 then
      local nspr, ntiles = sprite_state()
      spr_ok = (nspr >= MINSPR) and (ntiles >= 8)
    end
    local go_ok = (not GW_GO) or go_present()
    if marker_ok and spr_ok and go_ok then
      started = frame
      emu.log(string.format("mesen_frames: watch hit at frame %d", frame))
      local m = io.open(DIR .. "/started.txt", "w")
      if m then m:write(tostring(frame) .. "\n"); m:close() end
    end
  end
  local lo = started or FROM
  if (started or (not wAddr and MINSPR == 0 and not GW_GO)) and frame >= lo and frame < lo + COUNT then
    clear_once()
    local png = emu.takeScreenshot()
    if png then
      local f = io.open(string.format("%s/f%06d.png", DIR, frame), "wb")
      if f then f:write(png); f:close() end
    end
    -- OAM beside the picture. When a frame differs, the picture says WHERE and
    -- the sprite table says WHY: a plume that is absent from OAM was never
    -- emitted by the game, while one present at the wrong coordinates was
    -- emitted and misplaced. Those are different defects with different fixes,
    -- and only the table separates them. 544 bytes: 512 low + 32 high.
    if GW_OAM then
      local oa = {}
      for i = 0, 543 do
        oa[#oa + 1] = string.char(emu.read(i, emu.memType.snesSpriteRam) % 256)
      end
      local af = io.open(string.format("%s/f%06d_oam.bin", DIR, frame), "wb")
      if af then af:write(table.concat(oa)); af:close() end
    end

    if GW_CGRAM then
      local cg = {}
      for i = 0, 511 do
        cg[#cg + 1] = string.char(emu.read(i, emu.memType.snesCgRam) % 256)
      end
      local cf = io.open(string.format("%s/f%06d_cgram.bin", DIR, frame), "wb")
      if cf then cf:write(table.concat(cg)); cf:close() end
    end

    if GW_WRAM then
      local wr = {}
      for i = 0, 0x1FFF do
        wr[#wr + 1] = string.char(emu.read(i, emu.memType.snesWorkRam) % 256)
      end
      local wf = io.open(string.format("%s/f%06d_wram.bin", DIR, frame), "wb")
      if wf then wf:write(table.concat(wr)); wf:close() end
    end

    if GW_VRAM then
      local vr = {}
      for i = VLO, VHI do
        vr[#vr + 1] = string.char(emu.read(i, emu.memType.snesVideoRam) % 256)
      end
      local vf = io.open(string.format("%s/f%06d_vram.bin", DIR, frame), "wb")
      if vf then vf:write(table.concat(vr)); vf:close() end
    end
  end
end, emu.eventType.endFrame)

local m = io.open(DIR .. "/loaded.txt", "w")
if m then m:write(string.format("frames %d..%d\n", FROM, FROM+COUNT-1)); m:close() end
