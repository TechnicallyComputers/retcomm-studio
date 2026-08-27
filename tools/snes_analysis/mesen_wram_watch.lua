-- mesen_wram_watch.lua — per-frame WRAM timeline from Mesen2.
--
-- The reference side of "does this variable ever take the value ours never
-- reaches". snesrecomp's debug server can sample its own WRAM over TCP, but a
-- sample is not a timeline: a value that ramps 0->15 over 16 frames and a value
-- stuck at 0 look identical if you only catch one frame of each. This writes
-- one CSV row per frame so the two sides can be diffed as sequences.
--
-- Run:
--     mesen-ce <rom> mesen_wram_watch.lua
--
-- Environment (all optional):
--     GW_ADDRS   comma-separated WRAM offsets in hex (default 0100,0010,000E)
--                These are $7E bank offsets, not CPU-bus addresses.
--     GW_OUT     output CSV      (default /tmp/mesen_wram.csv)
--     GW_UNTIL   stop after this frame (default 6000)
--
-- Requires Debug > ScriptWindow > AllowIoOsAccess = true, set while Mesen is
-- NOT running (it rewrites settings.json on exit).

local OUT = os.getenv("GW_OUT") or "/tmp/mesen_wram.csv"
local UNTIL = tonumber(os.getenv("GW_UNTIL") or "") or 6000
local spec = os.getenv("GW_ADDRS") or "0100,0010,000E"

local addrs = {}
for tok in string.gmatch(spec, "[^,]+") do
  local v = tonumber((tok:gsub("%s+", "")), 16)
  if v then addrs[#addrs + 1] = v end
end

local csv = io.open(OUT, "w")
if not csv then
  emu.log("mesen_wram_watch: cannot open " .. OUT)
  return
end
local header = "frame"
for _, a in ipairs(addrs) do header = header .. string.format(",w%04X", a) end
csv:write(header .. "\n")
csv:flush()

local frame = 0

-- Wrapped: a wrong memType name would otherwise throw inside the frame callback
-- and cost every row silently, which is how this class of script usually fails
-- (header written, no data).
local warned = false
local function wram(addr)
  local ok, v = pcall(function()
    return emu.read(addr, emu.memType.snesWorkRam)
  end)
  if ok and type(v) == "number" then return v end
  if not warned then
    warned = true
    emu.log("mesen_wram_watch: WRAM read failed: " .. tostring(v))
  end
  return -1
end

emu.addEventCallback(function()
  frame = frame + 1
  if frame > UNTIL then return end
  local row = tostring(frame)
  for _, a in ipairs(addrs) do row = row .. "," .. tostring(wram(a)) end
  csv:write(row .. "\n")
  if (frame % 60) == 0 then csv:flush() end
end, emu.eventType.endFrame)

emu.log(string.format("mesen_wram_watch: %d addresses -> %s", #addrs, OUT))
