-- mesen_scene_sample.lua — periodic PPU-state + screenshot sampler for Mesen2.
--
-- Companion to mesen_raster_trace.lua. Where that one answers "at which
-- scanline did a register change", this one answers "what was the whole PPU
-- configured like, and what did the screen look like" at a cadence, so a run
-- can be aligned against snesrecomp's own `get_ppu_state` / `screenshot` by
-- scene rather than by frame number. The two emulators do not stay
-- frame-locked, so matching on picture is the only honest alignment.
--
-- Run:
--     mesen-ce <rom> mesen_scene_sample.lua
--
-- Environment (all optional):
--     GW_DIR      output directory      (default /tmp/mesen_scene)
--     GW_EVERY    frames per CSV row    (default 60)
--     GW_SHOT     frames per screenshot (default 300)
--     GW_UNTIL    stop after this frame (default 20000)
--
-- Requires Debug > ScriptWindow > AllowIoOsAccess = true, set while Mesen is
-- NOT running (it rewrites settings.json on exit).
--
-- Field names verified against MesenCE 2.2.1: emu.getState() is a FLAT table
-- with dotted keys, so st["ppu.mainScreenLayers"], not st.ppu.mainScreenLayers.

local DIR = os.getenv("GW_DIR") or "/tmp/mesen_scene"
local EVERY = tonumber(os.getenv("GW_EVERY") or "") or 60
local SHOT = tonumber(os.getenv("GW_SHOT") or "") or 300
local UNTIL = tonumber(os.getenv("GW_UNTIL") or "") or 20000

os.execute('mkdir -p "' .. DIR .. '"')

local csvPath = DIR .. "/scene.csv"
local csv = io.open(csvPath, "w")
csv:write("frame,forcedBlank,brightness,bgMode,bg3Prio,TM,TS,"
       .. "cmEnabled,cmAddSub,cmHalve,cmSubtract,fixedColor,"
       .. "bg1_map,bg1_chr,bg1_hs,bg1_vs,"
       .. "bg2_map,bg2_chr,bg2_hs,bg2_vs,"
       .. "bg3_map,bg3_chr,bg3_hs,bg3_vs,"
       .. "w0l,w0r,w1l,w1r,"
       -- Game-side selectors, so a divergence can be traced to the branch that
       -- caused it rather than to the register it ended up writing:
       --   $10 = index the NMI handler dispatches on (JSR ($83AE,X) at $00:83A2)
       --   $0A = NMI counter, $0E = vblank flag the main loop waits on
       .. "wram10,wram0A,wram0E\n")
csv:flush()

local frame = 0

-- WRAM reads are wrapped: a wrong memType name would otherwise throw inside the
-- frame callback and silently cost us every row, which is how this first
-- presented itself (header written, no data).
local readWarned = false
local function wram(addr)
  local ok, v = pcall(function()
    return emu.read(addr, emu.memType.snesWorkRam)
  end)
  if ok and type(v) == "number" then return v end
  local ok2, v2 = pcall(function()
    return emu.read(0x7E0000 + addr, emu.memType.snesMemory)
  end)
  if ok2 and type(v2) == "number" then return v2 end
  if not readWarned then
    readWarned = true
    emu.log("mesen_scene_sample: WRAM read failed: " .. tostring(v) ..
            " / " .. tostring(v2))
  end
  return -1
end

local function n(st, k)
  local v = st[k]
  if v == nil then return -1 end
  if v == true then return 1 end
  if v == false then return 0 end
  return v
end

local function onFrame()
  frame = frame + 1
  if frame > UNTIL then return end

  if (frame % EVERY) == 0 then
    local st = emu.getState()
    csv:write(string.format(
      "%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d," ..
      "%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d," ..
      "%d,%d,%d\n",
      frame,
      n(st, "ppu.forcedBlank"), n(st, "ppu.screenBrightness"),
      n(st, "ppu.bgMode"), n(st, "ppu.mode1Bg3Priority"),
      n(st, "ppu.mainScreenLayers"), n(st, "ppu.subScreenLayers"),
      n(st, "ppu.colorMathEnabled"), n(st, "ppu.colorMathAddSubscreen"),
      n(st, "ppu.colorMathHalveResult"), n(st, "ppu.colorMathSubtractMode"),
      n(st, "ppu.fixedColor"),
      n(st, "ppu.layers[0].tilemapAddress"), n(st, "ppu.layers[0].chrAddress"),
      n(st, "ppu.layers[0].hscroll"), n(st, "ppu.layers[0].vscroll"),
      n(st, "ppu.layers[1].tilemapAddress"), n(st, "ppu.layers[1].chrAddress"),
      n(st, "ppu.layers[1].hscroll"), n(st, "ppu.layers[1].vscroll"),
      n(st, "ppu.layers[2].tilemapAddress"), n(st, "ppu.layers[2].chrAddress"),
      n(st, "ppu.layers[2].hscroll"), n(st, "ppu.layers[2].vscroll"),
      n(st, "ppu.window[0].left"), n(st, "ppu.window[0].right"),
      n(st, "ppu.window[1].left"), n(st, "ppu.window[1].right"),
      wram(0x10), wram(0x0A), wram(0x0E)))
    csv:flush()
  end

  if (frame % SHOT) == 0 then
    local png = emu.takeScreenshot()
    if png then
      local f = io.open(string.format("%s/shot_%06d.png", DIR, frame), "wb")
      if f then f:write(png); f:close() end
    end
  end

  if (frame % 1200) == 0 then
    emu.log("mesen_scene_sample: frame " .. frame)
  end
end

emu.addEventCallback(onFrame, emu.eventType.endFrame)
emu.log("mesen_scene_sample: writing " .. csvPath)
