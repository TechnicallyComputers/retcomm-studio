-- mesen_raster_trace.lua — beam-stamped PPU/IRQ register timeline, from Mesen2.
--
-- The measurement our own runtime cannot make. snesrecomp's debug server
-- records register writes as (frame, addr, value) with no beam position, so it
-- can say *what* a game wrote but never *at which scanline*. For a title that
-- raster-splits the screen that is the whole question.
--
-- Run:
--     mesen-ce <rom> mesen_raster_trace.lua
--
-- Environment (all optional):
--     GW_FRAME   frame to capture          (default 2500)
--     GW_OUT     output CSV                (default /tmp/mesen_raster.csv)
--     GW_SPAN    frames to capture         (default 1)
--
-- Requires Debug > ScriptWindow > AllowIoOsAccess = true in Mesen's
-- settings.json, set while Mesen is NOT running — it rewrites that file on
-- exit and will otherwise clobber the change.
--
-- Notes on the API, verified against MesenCE 2.2.1 rather than assumed:
--   * emu.getState() returns a FLAT table with dotted keys, so it is
--     st["ppu.scanline"], not st.ppu.scanline.
--   * there is no ppu.cycle; memoryManager.hClock is the horizontal position.
--   * emu.addMemoryCallback(fn, emu.callbackType.write, lo, hi) covers the CPU
--     address space, which is where $21xx/$42xx live.

local TARGET = tonumber(os.getenv("GW_FRAME") or "") or 2500
local SPAN = tonumber(os.getenv("GW_SPAN") or "") or 1
local OUT = os.getenv("GW_OUT") or "/tmp/mesen_raster.csv"

-- Registers that decide how a scanline is composed, plus the raster-split
-- timers that schedule the changes.
local WATCH = {
  [0x2100] = "INIDISP", [0x2105] = "BGMODE",  [0x2106] = "MOSAIC",
  [0x2107] = "BG1SC",   [0x2108] = "BG2SC",   [0x2109] = "BG3SC",
  [0x210B] = "BG12NBA", [0x210C] = "BG34NBA",
  [0x210D] = "BG1HOFS", [0x210E] = "BG1VOFS",
  [0x210F] = "BG2HOFS", [0x2110] = "BG2VOFS",
  [0x2111] = "BG3HOFS", [0x2112] = "BG3VOFS",
  [0x2123] = "W12SEL",  [0x2124] = "W34SEL",  [0x2125] = "WOBJSEL",
  [0x2126] = "WH0",     [0x2127] = "WH1",     [0x2128] = "WH2", [0x2129] = "WH3",
  [0x212C] = "TM",      [0x212D] = "TS",
  [0x212E] = "TMW",     [0x212F] = "TSW",
  [0x2130] = "CGWSEL",  [0x2131] = "CGADSUB", [0x2132] = "COLDATA",
  [0x4200] = "NMITIMEN",
  [0x4207] = "HTIMEL",  [0x4208] = "HTIMEH",
  [0x4209] = "VTIMEL",  [0x420A] = "VTIMEH",
  [0x420B] = "MDMAEN",  [0x420C] = "HDMAEN",
}

local rows = {}
local frame = 0
local armed = false
local done = false

local function onWrite(addr, value)
  if not armed or done then return end
  local name = WATCH[addr]
  if not name then return end
  local st = emu.getState()
  rows[#rows + 1] = string.format("%d,%d,%d,%04X,%s,%02X",
    frame,
    st["ppu.scanline"] or -1,
    st["memoryManager.hClock"] or -1,
    addr, name, value)
end

local function flush()
  local f = io.open(OUT, "w")
  if not f then
    emu.log("mesen_raster_trace: cannot open " .. OUT)
    return
  end
  f:write("frame,scanline,hclock,addr,reg,value\n")
  for _, r in ipairs(rows) do f:write(r .. "\n") end
  f:close()
  emu.log(string.format("mesen_raster_trace: wrote %d rows to %s", #rows, OUT))
end

local function onFrame()
  frame = frame + 1
  if done then return end
  if frame == TARGET then
    armed = true
    emu.log("mesen_raster_trace: capturing from frame " .. frame)
  elseif armed and frame > TARGET + SPAN - 1 then
    armed = false
    done = true
    flush()
  elseif (frame % 300) == 0 and not armed then
    emu.log("mesen_raster_trace: at frame " .. frame .. ", waiting for " .. TARGET)
  end
end

emu.addMemoryCallback(onWrite, emu.callbackType.write, 0x2100, 0x213F)
emu.addMemoryCallback(onWrite, emu.callbackType.write, 0x4200, 0x420F)
emu.addEventCallback(onFrame, emu.eventType.endFrame)
emu.log(string.format("mesen_raster_trace: armed for frame %d (+%d), out=%s",
                      TARGET, SPAN, OUT))
