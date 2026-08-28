-- gw_entry_probe.lua — capture the SCENE ENTRY, to find what pins the phase
-- bit between the pass counter $08 and the per-object animation countdown
-- $1580,X.  Traces writes to the animation script cursor ($1502,X), the
-- countdown ($1580,X) and $08 from frame 1, so the FIRST tick of the mechs'
-- script is on record together with $08's parity at that instant.
local DIR    = os.getenv("GW_DIR") or "/tmp/gw_entry"
local COUNT  = tonumber(os.getenv("GW_COUNT") or "") or 5200
local IDLE   = tonumber(os.getenv("GW_IDLE") or "") or 90
local PARITY = tonumber(os.getenv("GW_PARITY") or "") or 0
local TAIL   = tonumber(os.getenv("GW_TAIL") or "") or 30
os.execute('mkdir -p "' .. DIR .. '"')

local frame, lastc08, idle, cool, presses = 0, -1, 0, 0, 0
local press_until = -1
local lagrun, blocks, stage = 0, {}, "menus"
local confirm_done = false
local ring = {}
local animating_at = -1
local stopped = false

local wlog = io.open(DIR .. "/writes.csv", "w")
wlog:write("frame,addr,value,pc,c08\n"); wlog:flush()
local flog = io.open(DIR .. "/frames.csv", "w")
flog:write("frame,c08,t1580,t1584,c1502,c1506,p1302,p1306,f0,cur20\n")
local plog = io.open(DIR .. "/presses.txt", "w")

local wrows = 0
local function onWrite(addr, value)
  if stopped or wrows > 60000 then return end
  local st = emu.getState()
  local c08 = emu.read(0x08, emu.memType.snesWorkRam)
             + 256 * emu.read(0x09, emu.memType.snesWorkRam)
  wlog:write(string.format("%d,%06X,%02X,%02X:%04X,%d\n", frame, addr,
    value % 256, st["cpu.k"] or 255, st["cpu.pc"] or 0, c08))
  wrows = wrows + 1
  if (wrows % 64) == 0 then wlog:flush() end
end

do local f = io.open(DIR .. "/loaded", "w") if f then f:write("loaded\n") f:close() end end
-- $1500/$1580/$1302 are written with DBR=$7E (STA $1500,X after a JSL that
-- never sets DBR), so they never appear at the bank-00 mirror a plain
-- 0x1500-0x1507 callback watches -- measured: 800 frames, zero hits, while
-- the direct-page $0008 callback fired every frame.  Register BOTH the
-- bank-00 CPU address and the $7E:xxxx form.
-- $2181-$2183 = WMADDL/M/H (the WRAM DMA destination) and $420B = the MDMA
-- trigger.  $08/$09 is stamped at the scene start by something that is NOT a
-- CPU write (callbacks see only the INC at $00:80CA), so a WRAM-targeted DMA
-- is the prime suspect: catch the frame where WMADD is pointed at $0008.
-- $2180 itself is deliberately NOT watched -- it is the data port and far too
-- hot to log.
local dlog = io.open(DIR .. "/dma.csv", "w")
dlog:write("frame,addr,value,pc,c08,wmadd,chan_b,chan_a,chan_sz\n"); dlog:flush()
local function onDma(addr, value)
  if stopped then return end
  local st = emu.getState()
  local c08 = emu.read(0x08, emu.memType.snesWorkRam)
             + 256 * emu.read(0x09, emu.memType.snesWorkRam)
  local wm = emu.read(0x2181, emu.memType.snesMemory)
           + 256 * emu.read(0x2182, emu.memType.snesMemory)
           + 65536 * emu.read(0x2183, emu.memType.snesMemory)
  local bb, aa, sz = "", "", ""
  if (addr % 0x10000) == 0x420B then
    for ch = 0, 7 do
      if (value % 256) >= 0 and ((value % 256) & (1 << ch)) ~= 0 then
        bb = bb .. string.format("%02X ", emu.read(0x4301 + ch * 0x10, emu.memType.snesMemory))
        aa = aa .. string.format("%02X%02X%02X ",
              emu.read(0x4304 + ch * 0x10, emu.memType.snesMemory),
              emu.read(0x4303 + ch * 0x10, emu.memType.snesMemory),
              emu.read(0x4302 + ch * 0x10, emu.memType.snesMemory))
        sz = sz .. string.format("%02X%02X ",
              emu.read(0x4306 + ch * 0x10, emu.memType.snesMemory),
              emu.read(0x4305 + ch * 0x10, emu.memType.snesMemory))
      end
    end
  end
  dlog:write(string.format("%d,%04X,%02X,%02X:%04X,%d,%06X,%s,%s,%s\n",
    frame, addr % 0x10000, value % 256, st["cpu.k"] or 255, st["cpu.pc"] or 0,
    c08, wm, bb, aa, sz))
end
for _, rg in ipairs({ {0x2181,0x2183}, {0x420B,0x420B} }) do
  local ok, err = pcall(function()
    emu.addMemoryCallback(onDma, emu.callbackType.write, rg[1], rg[2])
  end)
  local f = io.open(DIR .. "/loaded", "a")
  if f then f:write(string.format("dma %04X-%04X: %s\n", rg[1], rg[2],
    ok and "ok" or tostring(err))) f:close() end
end
for _, rg in ipairs({ {0x0008,0x0009}, {0x1500,0x1507}, {0x1580,0x1587},
                      {0x1302,0x1307},
                      {0x7E1500,0x7E1507}, {0x7E1580,0x7E1587},
                      {0x7E1302,0x7E1307} }) do
  local ok, err = pcall(function()
    emu.addMemoryCallback(onWrite, emu.callbackType.write, rg[1], rg[2])
  end)
  local f = io.open(DIR .. "/loaded", "a")
  if f then f:write(string.format("%06X-%06X: %s\n", rg[1], rg[2],
    ok and "ok" or tostring(err))) f:close() end
end

emu.addEventCallback(function()
  frame = frame + 1
  -- Mesen AUTO-RESUMES its exit state, which here is the VS screen itself.
  -- Power-cycle on the first frame so this run really starts at boot and the
  -- scene entry is on record.
  if frame == 1 then
    local ok = pcall(function() emu.power() end)
    local f = io.open(DIR .. "/loaded", "a")
    if f then f:write("power(): " .. tostring(ok) .. "\n") f:close() end
  end
  if frame > COUNT or stopped then return end
  local r = function(a) return emu.read(a, emu.memType.snesWorkRam) end
  local c08 = r(0x08) + 256 * r(0x09)
  local t0, t1 = r(0x1580), r(0x1584)
  ring[#ring + 1] = { t0, t1 }
  if #ring > 6 then table.remove(ring, 1) end
  -- VS-screen-specific gate.  A bare "both timers alternate 0/1" test fires
  -- on the intro video too (measured: armed at frame 2219 on the attract
  -- reel).  The pose cells are scene-specific: $1302 in {12,14} and $1306 in
  -- {B1,B3} only on the pre-fight screen.
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
  flog:write(string.format("%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n",
    frame, c08, t0, t1, r(0x1502), r(0x1506), r(0x1302), r(0x1306),
    r(0xF0), r(0x20)))
  if animating and animating_at < 0 then
    animating_at = frame
    plog:write(string.format("ANIMATING at frame %d c08 %d\n", frame, c08))
    plog:flush()
    local png = emu.takeScreenshot()
    if png then local f = io.open(DIR .. "/scene.png", "wb")
      if f then f:write(png); f:close() end end
  end
  -- PIXELS.  The (F0,pose) census is only worth anything if it tracks what is
  -- on screen; a hardware run measured SPLIT in WRAM must be checked against
  -- its own frames.  Per-frame shots (a %4 cadence aliases a 4-cycle to one
  -- layout).
  if animating_at > 0 and frame <= animating_at + TAIL then
    local png = emu.takeScreenshot()
    if png then
      local f = io.open(string.format("%s/p%06d.png", DIR, frame), "wb")
      if f then f:write(png); f:close() end
    end
    -- Plume-art fingerprint: every 4th byte of OBJ tiles t1 4C-7F
    -- (VRAM 0x8980-0x9000) and t2 08-3F (0xA100-0xA800).  IDENTICAL stride to
    -- analysis/vram_class.py so the two emulators' art compares byte for byte,
    -- not merely by label.  864 reads/frame -- a full VRAM dump is ~1fps.
    local fp = {}
    for a = 0x8980, 0x8FFF, 4 do
      fp[#fp + 1] = string.char(emu.read(a, emu.memType.snesVideoRam) % 256)
    end
    for a = 0xA100, 0xA7FF, 4 do
      fp[#fp + 1] = string.char(emu.read(a, emu.memType.snesVideoRam) % 256)
    end
    local vf = io.open(string.format("%s/v%06d.bin", DIR, frame), "wb")
    if vf then vf:write(table.concat(fp)); vf:close() end
    -- Full OAM (512+32) per frame: the last unexcluded state.  Everything
    -- else (WRAM census, VRAM art, CGRAM, renderer) is measured identical.
    local oa = {}
    for i = 0, 543 do
      oa[#oa + 1] = string.char(emu.read(i, emu.memType.snesSpriteRam) % 256)
    end
    local of = io.open(string.format("%s/o%06d.bin", DIR, frame), "wb")
    if of then of:write(table.concat(oa)); of:close() end
  end
  if animating_at > 0 and frame >= animating_at + TAIL then
    stopped = true
    wlog:flush(); flog:flush(); dlog:flush()
    local f = io.open(DIR .. "/done", "w") if f then f:write("done\n") f:close() end
    emu.log("gw_entry: done")
  end
end, emu.eventType.endFrame)

emu.addEventCallback(function()
  local st = {}
  if frame <= press_until then st.start = true end
  pcall(function() emu.setInput(0, st) end)
end, emu.eventType.inputPolled)
