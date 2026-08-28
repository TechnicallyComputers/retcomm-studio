-- mesen_cycle_probe.lua — cycle/instruction accounting from Mesen2.
--
-- The question this answers: does the guest retire the same amount of WORK
-- per frame on both machines? snesrecomp's NMI count matches hardware exactly
-- while its game state runs ahead, which is the signature of instructions
-- costing the wrong number of cycles rather than of a frame-timing fault.
--
-- Writes, at each GW_DUMP frame:
--   cycles.csv  frame + every state key whose name mentions cycle/clock,
--               so per-frame deltas can be taken without guessing field names
--   keys.txt    the full emu.getState() key list, once (first dump only)
--
-- Run:
--     GW_DIR=... GW_DUMP=100,200,... mesen-ce <rom> mesen_cycle_probe.lua
--
-- Environment: GW_SCRIPT / GW_DUMP / GW_DIR / GW_UNTIL, as the sibling
-- scripts. Requires Debug > ScriptWindow > AllowIoOsAccess.

local DIR   = os.getenv("GW_DIR") or "/tmp/mesen_cycles"
local SPEC  = os.getenv("GW_SCRIPT") or ""
local DUMPS = os.getenv("GW_DUMP") or "100,200,300"
local UNTIL = tonumber(os.getenv("GW_UNTIL") or "") or 4000

os.execute('mkdir -p "' .. DIR .. '"')

local steps = {}
for f, b in string.gmatch(SPEC, "(%d+):([^,]*)") do
  steps[#steps + 1] = { frame = tonumber(f), btns = b }
end
table.sort(steps, function(a, b) return a.frame < b.frame end)

local dumpAt = {}
local order = {}
for f in string.gmatch(DUMPS, "%d+") do
  dumpAt[tonumber(f)] = true; order[#order + 1] = tonumber(f)
end

local LETTER = { a="a", b="b", x="x", y="y", l="l", r="r",
                 s="select", t="start", u="up", d="down", n="left", m="right" }
local frame = 0

local function currentSpec()
  local cur = ""
  for _, s in ipairs(steps) do if frame >= s.frame then cur = s.btns end end
  return cur
end

emu.addEventCallback(function()
  local st = {}
  for c in string.gmatch(currentSpec(), ".") do
    local k = LETTER[c]; if k then st[k] = true end
  end
  pcall(function() emu.setInput(0, st) end)
end, emu.eventType.inputPolled)

-- File handles opened lazily: io.open during the initial chunk returns nil
-- under Mesen's script sandbox (learned the hard way).
local csv, wroteKeys, cols = nil, false, nil

local function dump()
  local ok, state = pcall(emu.getState)
  if not ok or type(state) ~= "table" then return end

  if not wroteKeys then
    wroteKeys = true
    local names = {}
    for k, _ in pairs(state) do names[#names + 1] = k end
    table.sort(names)
    local kf = io.open(DIR .. "/keys.txt", "w")
    if kf then kf:write(table.concat(names, "\n") .. "\n"); kf:close() end
    -- Every numeric key that looks like a clock. Named rather than assumed,
    -- because the field set differs between Mesen builds.
    cols = {}
    for _, k in ipairs(names) do
      local lk = string.lower(k)
      if (string.find(lk, "cycle", 1, true) or string.find(lk, "clock", 1, true)
          or string.find(lk, "frame", 1, true))
         and type(state[k]) == "number" then
        cols[#cols + 1] = k
      end
    end
    csv = io.open(DIR .. "/cycles.csv", "w")
    if csv then csv:write("frame," .. table.concat(cols, ",") .. "\n"); csv:flush() end
  end

  if csv and cols then
    local row = { tostring(frame) }
    for _, k in ipairs(cols) do row[#row + 1] = tostring(state[k] or 0) end
    csv:write(table.concat(row, ",") .. "\n"); csv:flush()
  end
end

emu.addEventCallback(function()
  frame = frame + 1
  if frame > UNTIL then return end
  if dumpAt[frame] then dump() end
end, emu.eventType.endFrame)

local m = io.open(DIR .. "/loaded.txt", "w")
if m then m:write("cycle probe loaded, " .. #order .. " dump points\n"); m:close() end
