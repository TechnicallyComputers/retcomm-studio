-- gw_obsel_probe.lua v2 — real Mesen key names.
local DIR = os.getenv("GW_DIR") or "/tmp/gw_obsel"
local SLOT = os.getenv("GW_SLOT") or "1"
os.execute('mkdir -p "' .. DIR .. '"')
local frame, loaded = 0, false
local log = io.open(DIR .. "/regs.csv", "w")
log:write("frame,oamBase,oamOff,oamMode,oamRamAddr,internalOam,t1580,h1181\n")
emu.addEventCallback(function()
  frame = frame + 1
  if frame == 90 and not loaded then
    loaded = true
    local ok = pcall(function() emu.loadSaveState(tonumber(SLOT)) end)
    if not ok then ok = pcall(function() emu.loadSavestate(tonumber(SLOT)) end) end
    emu.log("load attempt: " .. tostring(ok))
  end
  if frame > 150 and frame <= 400 then
    local st = emu.getState()
    local r = function(a) return emu.read(a, emu.memType.snesWorkRam) end
    log:write(string.format("%d,%s,%s,%s,%s,%s,%d,%d\n", frame,
      tostring(st["ppu.oamBaseAddress"]), tostring(st["ppu.oamAddressOffset"]),
      tostring(st["ppu.oamMode"]), tostring(st["ppu.oamRamAddress"]),
      tostring(st["ppu.internalOamAddress"]), r(0x1580), r(0x1181)))
    if frame % 80 == 0 or frame == 400 then
      local png = emu.takeScreenshot()
      if png then
        local f = io.open(string.format("%s/s%06d.png", DIR, frame), "wb")
        if f then f:write(png); f:close() end
      end
    end
    if frame == 400 then log:close(); emu.log("done") end
  end
end, emu.eventType.endFrame)
