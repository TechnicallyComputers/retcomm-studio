-- gw_adaptive_probe.lua — drive to the VS screen with ADAPTIVE presses and a
-- parity knob. Fixed press schedules proved brittle: the flow shifts with
-- every press, so a frame list tuned for one run lands on the wrong screens
-- in the next. Instead: press Start only after the game has sat idle (no lag,
-- $08 advancing every frame) for GW_IDLE frames, at the first eligible frame
-- plus GW_PARITY (0 or 1). Stop pressing once the mechs animate (the
-- per-object timers at $1580/$1584 alternate 1,0). The char-select confirm is
-- then a fresh, isolated edge at a controlled parity.
local DIR    = os.getenv("GW_DIR") or "/tmp/gw_adaptive"
local COUNT  = tonumber(os.getenv("GW_COUNT") or "") or 5200
local IDLE   = tonumber(os.getenv("GW_IDLE") or "") or 90
local PARITY = tonumber(os.getenv("GW_PARITY") or "") or 0
os.execute('mkdir -p "' .. DIR .. '"')
local frame, lastc08, idle, cool, presses = 0, -1, 0, 0, 0
local press_until = -1
-- Char-select detector: the VS flow's signature is a >=30-frame lag block
-- followed within ~40 frames by a >=40-frame block (the 36+47 double load).
-- Only after both have passed does the final, parity-controlled confirm fire.
local lagrun, blocks, stage = 0, {}, "menus"
local confirm_done = false
local ring = {}
local vseen = {}
local log = io.open(DIR .. "/phase.csv", "w")
log:write("frame,c08,c0a,h1181,t1580,t1584,b0802,b0aa4,press,q22,cur20,cur21\n")
local plog = io.open(DIR .. "/presses.txt", "w")
emu.addEventCallback(function()
  frame = frame + 1
  if frame > COUNT then return end
  local r = function(a) return emu.read(a, emu.memType.snesWorkRam) end
  local c08 = r(0x08) + 256 * r(0x09)
  local t0, t1 = r(0x1580), r(0x1584)
  ring[#ring + 1] = { t0, t1 }
  if #ring > 6 then table.remove(ring, 1) end
  local animating = false
  if #ring == 6 then
    animating = true
    for i = 1, 6 do
      local a, b = ring[i][1], ring[i][2]
      if not ((a == 0 or a == 1) and (b == 0 or b == 1)) then animating = false end
    end
    if animating then
      for i = 1, 5 do
        if ring[i][1] == ring[i + 1][1] then animating = false end
      end
    end
  end
  if c08 == lastc08 then
    idle = 0; lagrun = lagrun + 1
  else
    if lagrun >= 25 then blocks[#blocks + 1] = { frame - lagrun, lagrun } end
    lagrun = 0
    idle = idle + 1
  end
  lastc08 = c08
  if stage == "menus" and #blocks >= 2 then
    local a, b = blocks[#blocks - 1], blocks[#blocks]
    if a[2] >= 30 and a[2] <= 39 and b[2] >= 40 and b[2] <= 52
       and frame > 600 and (b[1] - (a[1] + a[2])) < 40 then
      stage = "charselect"
      plog:write(string.format("charselect detected at frame %d\n", frame)); plog:flush()
    end
  end
  if cool > 0 then cool = cool - 1 end
  local pressing = frame <= press_until
  local want = false
  if stage == "menus" and presses < 4 and idle >= IDLE then want = true end
  if stage == "charselect" and not confirm_done and idle >= 150
     and (c08 % 2) == (PARITY % 2) then
    want = true
  end
  if not pressing and cool == 0 and want then
    press_until = frame + 8
    pressing = true
    cool = 90
    presses = presses + 1
    idle = 0
    if stage == "charselect" then confirm_done = true end
    plog:write(string.format("press %d (%s) at frame %d c08 %d par %d\n",
      presses, stage, frame, c08, frame % 2)); plog:flush()
  end
  -- pixels are the arbiter: once the mechs animate, save screenshots
  -- EVERY frame: a %4 cadence aliases a 4-frame cycle into one layout.
  if animating and presses > 0 then
    local png = emu.takeScreenshot()
    if png then
      local f = io.open(string.format("%s/s%06d.png", DIR, frame), "wb")
      if f then f:write(png); f:close() end
    end
    -- State fingerprint beside each shot: O-state from OAM slot 110's y
    -- (present = one pose), V-state from 64 bytes of the streamed plume art.
    -- A full VRAM dump is ~1 fps; this is free.
    local acc = ""
    for i = 0, 63 do
      acc = acc .. string.format("%02x", emu.read(0x8990 + i, emu.memType.snesVideoRam))
    end
    local sf = io.open(string.format("%s/s%06d.state", DIR, frame), "w")
    if sf then
      local oy = emu.read(110 * 4 + 1, emu.memType.snesSpriteRam)
      sf:write(string.format("%d %d %s\n", frame, oy, acc))
      sf:close()
    end
    -- full OAM per shot; full VRAM window + CGRAM once per V-state
    local oa = {}
    for i = 0, 543 do oa[#oa+1] = string.char(emu.read(i, emu.memType.snesSpriteRam) % 256) end
    local af = io.open(string.format("%s/s%06d_oam.bin", DIR, frame), "wb")
    if af then af:write(table.concat(oa)); af:close() end
    if not vseen[acc] then
      vseen[acc] = true
      local vr = {}
      for i = 0, 12287 do vr[#vr+1] = string.char(emu.read(0x8000 + i, emu.memType.snesVideoRam) % 256) end
      local vf = io.open(string.format("%s/v_%s.bin", DIR, string.sub(acc, 1, 12)), "wb")
      if vf then vf:write(table.concat(vr)); vf:close() end
      local cgd = {}
      for i = 0, 511 do cgd[#cgd+1] = string.char(emu.read(i, emu.memType.snesCgRam) % 256) end
      local cf = io.open(string.format("%s/c_%s.bin", DIR, string.sub(acc, 1, 12)), "wb")
      if cf then cf:write(table.concat(cgd)); cf:close() end
    end
  end
  -- $22 is the guest's DMA work-queue write pointer; $0800/$0810 are the
  -- first bytes of queue entries 0 and 1. Sampled at endFrame, i.e. after the
  -- field's builders ran and before/at the vblank drain — the same vantage
  -- our ring dump uses. Question this answers: does hardware fill the queue
  -- EVERY frame (both builders per pass) or alternate like we do?
  -- $20 is the sprite walker's SAVED CURSOR ($00:A122 STX $20). Sampled at
  -- endFrame it shows where the frame's walk finished. Compare against our
  -- per-frame cursor chain: if hardware's walk reaches both mechs each frame
  -- and ours stops short, the walk is exiting early.
  log:write(string.format("%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n",
    frame, c08, r(0x0A) + 256 * r(0x0B), r(0x1181), t0, t1,
    r(0x0802), r(0x0AA4), pressing and 1 or 0,
    r(0x22), r(0x20), r(0x21)))
  if frame == COUNT then
    log:close(); plog:close(); emu.log("gw_adaptive: done")
  end
end, emu.eventType.endFrame)
emu.addEventCallback(function()
  local st = {}
  if frame <= press_until then st.start = true end
  pcall(function() emu.setInput(0, st) end)
end, emu.eventType.inputPolled)
