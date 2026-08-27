-- mesen_wram_writers.lua — who writes a WRAM location, from Mesen2.
--
-- The producer side of a MISSING-effect diagnosis. snesrecomp's wram watch can
-- show OUR writers; this shows the reference's, with the writing PC — so "we
-- never write slot X" becomes "the reference writes it from $bb:pppp and we
-- never execute that code", which is a code-path divergence, not a mystery.
--
-- Run:
--     mesen-ce <rom> mesen_wram_writers.lua
--
-- Environment:
--     GW_ADDR    WRAM offset in hex, $7E bank (default 09A4)
--     GW_SPAN    also watch this many further bytes (default 2)
--     GW_OUT     output CSV (default /tmp/mesen_writers.csv)
--     GW_UNTIL   stop after this frame (default 9000)
--
-- Watches both CPU-bus mirrors: $7E:xxxx and the bank-0 low-RAM mirror $00:xxxx
-- (for offsets < $2000). Requires AllowIoOsAccess (set while Mesen is stopped).

local ADDR = tonumber(os.getenv("GW_ADDR") or "09A4", 16)
local SPAN = tonumber(os.getenv("GW_SPAN") or "") or 2
local OUT = os.getenv("GW_OUT") or "/tmp/mesen_writers.csv"
local UNTIL = tonumber(os.getenv("GW_UNTIL") or "") or 9000
local HB = OUT .. ".hb"

local csv = io.open(OUT, "w")
if not csv then emu.log("mesen_wram_writers: cannot open " .. OUT) return end
csv:write("frame,addr,value,pc\n")
csv:flush()

local frame = 0
local rows = 0

local function onWrite(addr, value)
  if frame > UNTIL or rows > 20000 then return end
  local st = emu.getState()
  local pc = string.format("%02X:%04X",
    st["cpu.k"] or 255, st["cpu.pc"] or 0)
  csv:write(string.format("%d,%06X,%02X,%s\n", frame, addr, value, pc))
  rows = rows + 1
  if (rows % 32) == 0 then csv:flush() end
end

-- Load marker BEFORE any registration: a throwing addMemoryCallback would
-- otherwise kill the script silently, and headless there is no other signal.
do
  local f = io.open(OUT .. ".loaded", "w")
  if f then f:write("loaded\n"); f:close() end
end

local function reg(lo, hi, what)
  local ok, err = pcall(function()
    emu.addMemoryCallback(onWrite, emu.callbackType.write, lo, hi)
  end)
  local f = io.open(OUT .. ".loaded", "a")
  if f then
    f:write(string.format("%s [%06X-%06X]: %s\n", what, lo, hi,
                          ok and "ok" or tostring(err)))
    f:close()
  end
end

if ADDR < 0x2000 then
  reg(ADDR, ADDR + SPAN - 1, "bank0 mirror")
end
reg(0x7E0000 + ADDR, 0x7E0000 + ADDR + SPAN - 1, "wram $7E")

emu.addEventCallback(function()
  frame = frame + 1
  if frame == UNTIL then csv:flush() end
  -- Heartbeat: emu.log is invisible headless, and a CSV with only a header
  -- cannot distinguish "no writes" from "script never loaded".
  if (frame % 300) == 0 then
    local f = io.open(HB, "w")
    if f then f:write(string.format("frame=%d rows=%d\n", frame, rows)); f:close() end
  end
end, emu.eventType.endFrame)

emu.log(string.format("mesen_wram_writers: $7E:%04X +%d -> %s", ADDR, SPAN, OUT))
