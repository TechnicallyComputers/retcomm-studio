-- gw_phase_probe.lua — scripted power-on run logging the sprite-phase state
-- per frame. Exists to answer: does the animation/hover pairing on HARDWARE
-- depend on the parity of the frame Start was pressed, or does the game
-- enforce alignment? Log columns per frame:
--   frame, $08, $0A, $1181, $1580, $1584, b0802, b0AA4
-- GW_DIR: output dir. GW_PRESS: comma list of frames to press Start (held 8f).
local DIR   = os.getenv("GW_DIR") or "/tmp/gw_phase"
local PRESS = os.getenv("GW_PRESS") or "300,600,900,1200,1500"
local COUNT = tonumber(os.getenv("GW_COUNT") or "") or 2600
os.execute('mkdir -p "' .. DIR .. '"')
local press = {}
for f in string.gmatch(PRESS, "(%d+)") do press[#press+1]=tonumber(f) end
local frame = 0
local log = io.open(DIR .. "/phase.csv", "w")
log:write("frame,c08,c0a,h1181,t1580,t1584,b0802,b0aa4\n")
emu.addEventCallback(function()
  frame = frame + 1
  if frame <= COUNT then
    local r = function(a) return emu.read(a, emu.memType.snesWorkRam) end
    log:write(string.format("%d,%d,%d,%d,%d,%d,%d,%d\n",
      frame, r(0x08)+256*r(0x09), r(0x0A)+256*r(0x0B),
      r(0x1181), r(0x1580), r(0x1584), r(0x0802), r(0x0AA4)))
    if frame % 200 == 0 then
      local png = emu.takeScreenshot()
      if png then
        local f = io.open(string.format("%s/s%06d.png", DIR, frame), "wb")
        if f then f:write(png); f:close() end
      end
    end
    if frame == COUNT then log:close(); emu.log("gw_phase_probe: done") end
  end
end, emu.eventType.endFrame)
emu.addEventCallback(function()
  local st = {}
  for _, pf in ipairs(press) do
    if frame >= pf and frame < pf + 8 then st.start = true end
  end
  pcall(function() emu.setInput(0, st) end)
end, emu.eventType.inputPolled)
