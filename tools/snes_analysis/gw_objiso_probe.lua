-- gw_objiso_probe.lua — load a savestate by PATH (the slot-int API calls
-- silently did nothing: emu.loadSavestate takes a path), then capture the
-- OBJ-isolated screen per frame with a state fingerprint per shot.
local DIR  = os.getenv("GW_DIR") or "/tmp/gw_objiso"
local MSS  = os.getenv("GW_MSS") or ""
os.execute('mkdir -p "' .. DIR .. '"')
local frame, loaded = 0, false
emu.addEventCallback(function()
  frame = frame + 1
  if frame == 60 and not loaded then
    loaded = true
    local ok, err = pcall(function() emu.loadSavestate(MSS) end)
    emu.log("loadSavestate(path) -> " .. tostring(ok) .. " " .. tostring(err))
  end
  if frame > 130 and frame <= 330 then
    local png = emu.takeScreenshot()
    if png then
      local f = io.open(string.format("%s/s%06d.png", DIR, frame), "wb")
      if f then f:write(png); f:close() end
    end
    local sf = io.open(string.format("%s/s%06d.state", DIR, frame), "w")
    if sf then
      local oy = emu.read(110 * 4 + 1, emu.memType.snesSpriteRam)
      local acc = ""
      for i = 0, 63 do
        acc = acc .. string.format("%02x", emu.read(0x8990 + i, emu.memType.snesVideoRam))
      end
      sf:write(string.format("%d %d %s\n", frame, oy, acc)); sf:close()
    end
    if frame == 330 then emu.log("objiso done") end
  end
end, emu.eventType.endFrame)
