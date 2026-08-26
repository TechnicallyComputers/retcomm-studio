-- mesen_exec_probe.lua — does the reference ever EXECUTE a given PC range?
--
-- The other half of a "we never reach this code" claim. snesrecomp's debug
-- server can show which blocks our runtime executed (`trace_blocks_range`),
-- but a range being absent there only means something if the reference does
-- run it. This answers that side.
--
-- Run:
--     mesen-ce <rom> mesen_exec_probe.lua
--
-- Environment:
--     GW_LO / GW_HI   CPU-bus range to watch (hex, default 8950/8A10)
--     GW_OUT          output file (default /tmp/mesen_exec.txt)
--     GW_UNTIL        stop after this frame (default 6000)
--
-- Reports total hits, first/last frame, and the distinct PCs hit — enough to
-- tell "never runs" from "runs but rarely" from "runs every frame".

local LO = tonumber(os.getenv("GW_LO") or "8950", 16)
local HI = tonumber(os.getenv("GW_HI") or "8A10", 16)
local OUT = os.getenv("GW_OUT") or "/tmp/mesen_exec.txt"
local UNTIL = tonumber(os.getenv("GW_UNTIL") or "") or 6000

local hits = 0
local firstFrame, lastFrame = nil, nil
local pcs = {}
local frame = 0
local written = false

emu.addMemoryCallback(function(addr, value)
  hits = hits + 1
  if not firstFrame then firstFrame = frame end
  lastFrame = frame
  pcs[addr] = (pcs[addr] or 0) + 1
end, emu.callbackType.exec, LO, HI)

local function flush()
  local f = io.open(OUT, "w")
  if not f then return end
  f:write(string.format("range=$%04X-$%04X  frames=%d\n", LO, HI, frame))
  f:write(string.format("hits=%d firstFrame=%s lastFrame=%s\n",
                        hits, tostring(firstFrame), tostring(lastFrame)))
  local keys = {}
  for k in pairs(pcs) do keys[#keys + 1] = k end
  table.sort(keys)
  for _, k in ipairs(keys) do
    f:write(string.format("  $%04X x%d\n", k, pcs[k]))
  end
  if hits == 0 then f:write("  (never executed)\n") end
  f:close()
end

emu.addEventCallback(function()
  frame = frame + 1
  if (frame % 600) == 0 then flush() end
  if frame >= UNTIL and not written then
    written = true
    flush()
    emu.log("mesen_exec_probe: done, " .. hits .. " hits")
  end
end, emu.eventType.endFrame)

emu.log(string.format("mesen_exec_probe: watching $%04X-$%04X -> %s", LO, HI, OUT))
