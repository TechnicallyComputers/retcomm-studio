-- gw_scanout_probe.lua — sample OAM at SCANOUT time, not endFrame.
--
-- The endFrame OAM samples are proven untrustworthy for sub-frame timing
-- (Mesen's own screenshot/state skew; and our renderer cannot produce the
-- displayed 418/789 pictures from ANY endFrame state).  This probe samples
-- the full 544 bytes at emu.eventType.startFrame — the state the beam is
-- about to scan out — AND at endFrame of the same frame, so the two
-- vantages can be diffed directly, plus the per-frame screenshot.
--
-- Navigation is gw_entry_probe's (press Start after idle; VS-pose gate).
local DIR    = os.getenv("GW_DIR") or "/tmp/gw_scan"
local COUNT  = tonumber(os.getenv("GW_COUNT") or "") or 5200
local IDLE   = tonumber(os.getenv("GW_IDLE") or "") or 90
local PARITY = tonumber(os.getenv("GW_PARITY") or "") or 0
local TAIL   = tonumber(os.getenv("GW_TAIL") or "") or 24
os.execute('mkdir -p "' .. DIR .. '"')

local frame, lastc08, idle, cool, presses = 0, -1, 0, 0, 0
local press_until = -1
local lagrun, blocks, stage = 0, {}, "menus"
local confirm_done = false
local confirm_frame = -1
local postload = false
local ring = {}
local animating_at = -1
local stopped = false
local plog = io.open(DIR .. "/presses.txt", "w")
local flog = io.open(DIR .. "/frames.csv", "w")
flog:write("frame,c08,f0,cur20,p1302,p1306,t1580\n")

do local f = io.open(DIR .. "/loaded", "w") if f then f:write("loaded\n") f:close() end end

-- Scanline of every MDMA trigger ($420B).  Every state byte is measured
-- identical across emulators; the last free variable is WHERE in the frame
-- the OAM/art DMA lands relative to the beam.  bAdr per enabled channel
-- distinguishes OAM ($04), VRAM art ($18), CGRAM ($22).
local dlog = io.open(DIR .. "/dma.csv", "w")
dlog:write("frame,scanline,cycle,value,chans\n"); dlog:flush()
local dma_armed = false
pcall(function()
  emu.addMemoryCallback(function(addr, value)
    if not dma_armed or stopped then return end
    local st = emu.getState()
    local chans = ""
    for ch = 0, 7 do
      if (value % 256) & (1 << ch) ~= 0 then
        chans = chans .. string.format("%02X:", 
          emu.read(0x4301 + ch * 0x10, emu.memType.snesMemory))
      end
    end
    dlog:write(string.format("%d,%s,%s,%02X,%s\n", frame,
      tostring(st["ppu.scanline"]), tostring(st["ppu.cycle"]),
      value % 256, chans))
    dlog:flush()
  end, emu.callbackType.write, 0x420B, 0x420B)
end)

local function dump_oam(path)
  local oa = {}
  for i = 0, 543 do
    oa[#oa + 1] = string.char(emu.read(i, emu.memType.snesSpriteRam) % 256)
  end
  local f = io.open(path, "wb")
  if f then f:write(table.concat(oa)); f:close() end
end

-- startFrame: the OAM the beam scans out.  Registered separately; `frame`
-- is incremented in endFrame, so tag with frame+1 (this startFrame belongs
-- to the frame whose endFrame fires next).
-- OAM priority rotation ($2103 bit7 + OAM address) is NOT in the 544 bytes
-- and rotates which sprite index is highest-priority.  Sample it at both
-- vantages: it is the last hidden renderer input.
local rlog = io.open(DIR .. "/rot.csv", "w")
rlog:write("frame,vantage,enablePrio,oamRamAddress,internalOamAddress,oamBaseAddress,oamAddressOffset\n")
local function rot(vantage, tag)
  local st = emu.getState()
  rlog:write(string.format("%d,%s,%s,%s,%s,%s,%s\n", tag, vantage,
    tostring(st["ppu.enableOamPriority"]),
    tostring(st["ppu.oamRamAddress"]),
    tostring(st["ppu.internalOamAddress"]),
    tostring(st["ppu.oamBaseAddress"]),
    tostring(st["ppu.oamAddressOffset"])))
  rlog:flush()
end
-- OAM POKE MARKER: on chosen frames, write a distinctive sprite into slot 0
-- (offscreen y=224 in every game state; lowest index so it wins overlap) at
-- startFrame.  32x32 blob tile 0x80 (static bright white art, proven
-- byte-identical on both emulators) at x=8,y=8 — the dark starfield corner.
-- The game's own line-219 OAM DMA wipes it the same frame, so it exists for
-- exactly one scanout.  WHICH labeled picture shows it measures the
-- picture<->state binding directly, ending the screenshot-skew algebra.
local marker_frames = {}
local mlog = io.open(DIR .. "/marker.txt", "w")
emu.addEventCallback(function()
  if stopped or animating_at < 0 then return end
  if frame + 1 > animating_at + TAIL then return end
  local tag = frame + 1
  if marker_frames[tag] then
    emu.write(0, 8,    emu.memType.snesSpriteRam)  -- x
    emu.write(1, 8,    emu.memType.snesSpriteRam)  -- y
    emu.write(2, 0x80, emu.memType.snesSpriteRam)  -- tile (blob art)
    emu.write(3, 0x2A, emu.memType.snesSpriteRam)  -- attr (pal5 prio2)
    mlog:write(string.format("poked at startFrame tag %d\n", tag))
    mlog:flush()
  end
  dump_oam(string.format("%s/s%06d.bin", DIR, tag))
  rot("start", tag)
end, emu.eventType.startFrame)

emu.addEventCallback(function()
  frame = frame + 1
  if frame > COUNT or stopped then return end
  local r = function(a) return emu.read(a, emu.memType.snesWorkRam) end
  local c08 = r(0x08) + 256 * r(0x09)
  local t0, t1 = r(0x1580), r(0x1584)
  ring[#ring + 1] = { t0, t1 }
  if #ring > 6 then table.remove(ring, 1) end
  local p2, p6 = r(0x1302), r(0x1306)
  local vs_poses = (p2 == 0x12 or p2 == 0x14) and (p6 == 0xB1 or p6 == 0xB3)
  local animating = false
  if #ring == 6 and vs_poses then
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
    if lagrun >= 25 then
      blocks[#blocks + 1] = { frame - lagrun, lagrun }
      -- A load block AFTER the charselect confirm = the VS screen is being
      -- built for OUR entry.  Without this, a confirm that lands on the
      -- attract demo's already-running VS screen arms the gate instantly
      -- (measured: ANIMATING == press-5 frame; every genuine entry has a
      -- 120+ frame load gap).
      if confirm_done and frame - lagrun > confirm_frame then postload = true end
    end
    lagrun = 0; idle = idle + 1
  end
  lastc08 = c08
  -- Charselect detector: the VS flow's signature is a >=30-frame lag block
  -- followed within ~40 frames by a >=40-frame block (the 36+47 double load).
  -- Without this stage the pose gate fires on the ATTRACT REEL's own VS
  -- screen (measured: frame 1002, no charselect confirm ever pressed).
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
  if not pressing and cool == 0 and want and animating_at < 0 then
    press_until = frame + 8; pressing = true; cool = 90
    presses = presses + 1; idle = 0
    if stage == "charselect" then confirm_done = true; confirm_frame = frame end
    plog:write(string.format("press %d (%s) frame %d c08 %d\n",
      presses, stage, frame, c08)); plog:flush()
  end
  if animating_at > 0 then dma_armed = (frame <= animating_at + TAIL) end
  if animating and animating_at < 0 and confirm_done and postload then
    animating_at = frame
    for _, k in ipairs({ 6, 11, 16 }) do marker_frames[frame + k] = true end
    plog:write(string.format("ANIMATING at frame %d c08 %d\n", frame, c08))
    plog:flush()
    local png = emu.takeScreenshot()
    if png then local f = io.open(DIR .. "/scene.png", "wb")
      if f then f:write(png); f:close() end end
  end
  if animating_at > 0 and frame <= animating_at + TAIL then
    -- endFrame OAM + screenshot + WRAM key, same frame tag
    dump_oam(string.format("%s/e%06d.bin", DIR, frame))
    rot("end", frame)
    -- Blob tile art: OBJ tiles 0x80-0x93 = VRAM bytes 0x9000-0x9280.  These
    -- sit just past the plume fingerprint window and were never compared.
    -- Ours are STATIC (186/640 nonzero, all frames identical).  If hardware
    -- alternates them (art present <-> cleared) that is the missing effect.
    -- FULL OBJ VRAM (16KB, base word 0x4000 -> bytes 0x8000-0xBFFF) per
    -- frame.  The plume/blob fingerprints covered only slices; the pal-4
    -- sprites' tiles (bytes 0x8000-0x8200) were never compared at all.
    -- ~16K reads/frame is slow (~1fps) but the tail is only 24 frames.

    local png = emu.takeScreenshot()
    if png then
      local f = io.open(string.format("%s/p%06d.png", DIR, frame), "wb")
      if f then f:write(png); f:close() end
    end
    flog:write(string.format("%d,%d,%d,%d,%d,%d,%d\n",
      frame, c08, r(0xF0), r(0x20), p2, p6, t0))
    flog:flush()
  end
  if animating_at > 0 and frame >= animating_at + TAIL then
    stopped = true
    local f = io.open(DIR .. "/done", "w") if f then f:write("done\n") f:close() end
    emu.log("gw_scanout: done")
  end
end, emu.eventType.endFrame)

emu.addEventCallback(function()
  local st = {}
  if frame <= press_until then st.start = true end
  pcall(function() emu.setInput(0, st) end)
end, emu.eventType.inputPolled)
