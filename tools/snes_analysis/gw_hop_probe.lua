-- gw_hop_probe.lua — the "next hop": per-frame WRITE trace of the five
-- variables that carry the thruster artifact, with PCs, so hardware can be
-- compared instruction-for-instruction against our trace_wram output.
--
--   $0020  sprite-walker saved cursor  ($00:A122 STX $20 / $00:A329 init)
--   $00F0  the phase-opposite toggler found on our side ($03:CB59/$03:CB8A)
--   $07CE  per-object attribute cache, the DIRTY-CHECK slot ($00:A535)
--   $1181  mech A hover offset        ($03:C956 DEC / $03:C95B INC)
--   $1185  mech B hover offset        (same writers)
--
-- Navigation is gw_adaptive_probe's: press Start only after the game has sat
-- idle, stop once the mechs animate. Tracing arms ONLY once animating, so the
-- menus do not flood the log.
local DIR    = os.getenv("GW_DIR") or "/tmp/gw_hop"
local COUNT  = tonumber(os.getenv("GW_COUNT") or "") or 5200
local IDLE   = tonumber(os.getenv("GW_IDLE") or "") or 90
local PARITY = tonumber(os.getenv("GW_PARITY") or "") or 0
local TRACEN = tonumber(os.getenv("GW_TRACEN") or "") or 60   -- frames to trace
os.execute('mkdir -p "' .. DIR .. '"')

local frame, lastc08, idle, cool, presses = 0, -1, 0, 0, 0
local press_until = -1
local lagrun, blocks, stage = 0, {}, "menus"
local confirm_done = false
local ring = {}
local armed, arm_frame = false, -1

local wlog = io.open(DIR .. "/writes.csv", "w")
wlog:write("frame,addr,value,pc,c08\n"); wlog:flush()
local flog = io.open(DIR .. "/frames.csv", "w")
flog:write("frame,c08,cur20,f0,f1,c07ce,h1181,h1185,t1580,t1584,b0802,b0aa4\n")
local plog = io.open(DIR .. "/presses.txt", "w")

local wrows = 0
local function onWrite(addr, value)
  if not armed then return end
  if frame > arm_frame + TRACEN then return end
  local st = emu.getState()
  -- $08 read AT THE WRITE: tells whether the pass counter had already been
  -- INC'd ($00:80C9) when this variable was set.  That is the discriminator
  -- between "pose reads a stale $08" and "pose reads something else".
  local c08 = emu.read(0x08, emu.memType.snesWorkRam)
             + 256 * emu.read(0x09, emu.memType.snesWorkRam)
  wlog:write(string.format("%d,%04X,%02X,%02X:%04X,%d\n", frame, addr % 0x10000,
    value % 256, st["cpu.k"] or 255, st["cpu.pc"] or 0, c08))
  wrows = wrows + 1
  if (wrows % 32) == 0 then wlog:flush() end
end

do local f = io.open(DIR .. "/loaded", "w") if f then f:write("loaded\n") f:close() end end
for _, a in ipairs({ 0x0008, 0x0020, 0x00F0, 0x07CE, 0x1181, 0x1185 }) do
  local ok, err = pcall(function()
    emu.addMemoryCallback(onWrite, emu.callbackType.write, a, a)
  end)
  local f = io.open(DIR .. "/loaded", "a")
  if f then f:write(string.format("%04X: %s\n", a, ok and "ok" or tostring(err))) f:close() end
end

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
  if c08 == lastc08 then idle = 0; lagrun = lagrun + 1
  else
    if lagrun >= 25 then blocks[#blocks + 1] = { frame - lagrun, lagrun } end
    lagrun = 0; idle = idle + 1
  end
  lastc08 = c08
  if stage == "menus" and #blocks >= 2 then
    local a, b = blocks[#blocks - 1], blocks[#blocks]
    if a[2] >= 30 and a[2] <= 39 and b[2] >= 40 and b[2] <= 52
       and frame > 600 and (b[1] - (a[1] + a[2])) < 40 then
      stage = "charselect"
      plog:write(string.format("charselect at frame %d\n", frame)); plog:flush()
    end
  end
  if cool > 0 then cool = cool - 1 end
  local pressing = frame <= press_until
  local want = false
  if stage == "menus" and presses < 4 and idle >= IDLE then want = true end
  if stage == "charselect" and not confirm_done and idle >= 150
     and (c08 % 2) == (PARITY % 2) then want = true end
  if not pressing and cool == 0 and want then
    press_until = frame + 8; pressing = true; cool = 90
    presses = presses + 1; idle = 0
    if stage == "charselect" then confirm_done = true end
    plog:write(string.format("press %d (%s) frame %d c08 %d\n",
      presses, stage, frame, c08)); plog:flush()
  end
  -- Arm on the animation signature alone (frame > 60): Mesen auto-resumes its
  -- exit state, so a run can start ALREADY on the VS screen with no press.
  if animating and frame > 60 and not armed then
    armed = true; arm_frame = frame
    plog:write(string.format("ARMED at frame %d (presses %d)\n", frame, presses))
    plog:flush()
    local png = emu.takeScreenshot()
    if png then
      local f = io.open(DIR .. "/armed.png", "wb")
      if f then f:write(png); f:close() end
    end
  end
  if armed and frame <= arm_frame + TRACEN then
    flog:write(string.format("%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n",
      frame, c08, r(0x20), r(0xF0), r(0xF1), r(0x7CE), r(0x1181), r(0x1185),
      t0, t1, r(0x0802), r(0x0AA4)))
    flog:flush()
    if frame == arm_frame + TRACEN then
      wlog:flush()
      local f = io.open(DIR .. "/done", "w") if f then f:write("done\n") f:close() end
      emu.log("gw_hop: done")
    end
  end
end, emu.eventType.endFrame)

emu.addEventCallback(function()
  local st = {}
  if frame <= press_until then st.start = true end
  pcall(function() emu.setInput(0, st) end)
end, emu.eventType.inputPolled)
