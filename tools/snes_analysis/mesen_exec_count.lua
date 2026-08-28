-- mesen_exec_count.lua — how often does a guest routine actually RUN?
--
-- Cycle totals say how much work a machine did; they do not say how many
-- times a particular routine executed, and when two runs disagree about game
-- state while agreeing on NMI count, that count is the thing in question.
--
-- Logs, for each execution of each watched PC: the frame, the guest's own NMI
-- counter ($7E:000A) and the SPC/CPU clocks, so the recomp side can be
-- compared on the guest's clock rather than on a host frame number.
--
-- Run:
--     GW_PCS=00829C,0082A1 GW_DIR=... GW_UNTIL=400 mesen-ce <rom> this.lua
--
-- Environment: GW_PCS (comma-separated 24-bit hex PCs), GW_SCRIPT, GW_DIR,
-- GW_UNTIL. Requires Debug > ScriptWindow > AllowIoOsAccess.
--
-- Output is BUFFERED and flushed periodically, never per frame: a per-frame
-- write+flush was measured stalling the emulator dead after two frames while
-- leaving it looking alive.

local DIR   = os.getenv("GW_DIR") or "/tmp/mesen_exec"
local SPEC  = os.getenv("GW_SCRIPT") or ""
local PCS   = os.getenv("GW_PCS") or "00829C"
local UNTIL = tonumber(os.getenv("GW_UNTIL") or "") or 400

os.execute('mkdir -p "' .. DIR .. '"')

local steps = {}
for f, b in string.gmatch(SPEC, "(%d+):([^,]*)") do
  steps[#steps + 1] = { frame = tonumber(f), btns = b }
end
table.sort(steps, function(a, b) return a.frame < b.frame end)

local LETTER = { a="a", b="b", x="x", y="y", l="l", r="r",
                 s="select", t="start", u="up", d="down", n="left", m="right" }
local frame = 0
local rows = {}
local flushed = false

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

local function onExec(addr, value)
  if frame > UNTIL then return end
  local ok, nmi = pcall(function()
    return emu.read(0x0A, emu.memType.snesWorkRam)
  end)
  rows[#rows + 1] = string.format("%d,%06X,%d", frame, addr, ok and nmi or -1)
end

for pc in string.gmatch(PCS, "%x+") do
  local a = tonumber(pc, 16)
  emu.addMemoryCallback(onExec, emu.callbackType.exec, a, a)
end

local function flush()
  local f = io.open(DIR .. "/exec.csv", "w")
  if not f then return end
  f:write("frame,pc,nmi\n")
  for _, r in ipairs(rows) do f:write(r .. "\n") end
  f:close()
end

emu.addEventCallback(function()
  frame = frame + 1
  if (frame % 60) == 0 and frame <= UNTIL then flush() end
  if frame > UNTIL and not flushed then flushed = true; flush() end
end, emu.eventType.endFrame)

local m = io.open(DIR .. "/loaded.txt", "w")
if m then m:write("exec probe loaded for PCs " .. PCS .. "\n"); m:close() end
