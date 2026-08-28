-- mesen_vram_dump.lua — dump VRAM / CGRAM / OAM at a chosen frame, from Mesen2.
--
-- The reference side of a content diff against snesrecomp's debug server
-- (`dump_vram` / `dump_cgram` / `dump_oam`). Registers can match perfectly and
-- the picture still be wrong if what the tilemap points at differs, so this is
-- the comparison that separates "configured differently" from "drew from
-- different bytes".
--
-- Run:
--     mesen-ce <rom> mesen_vram_dump.lua
--
-- Environment (all optional):
--     GW_DIR     output directory  (default /tmp/mesen_dump)
--     GW_FRAME   frame to dump at  (default 7500 — inside the gameplay demo)
--     GW_EVERY   also re-dump every N frames after that (0 = once, default 0)
--
-- Requires Debug > ScriptWindow > AllowIoOsAccess = true, set while Mesen is
-- NOT running (it rewrites settings.json on exit).
--
-- A screenshot is written alongside each dump: the attract loop is long and
-- frame numbers drift between emulators, so the picture is the only reliable
-- proof that a dump was taken in the scene we think it was.

local DIR = os.getenv("GW_DIR") or "/tmp/mesen_dump"
local FRAME = tonumber(os.getenv("GW_FRAME") or "") or 7500
local EVERY = tonumber(os.getenv("GW_EVERY") or "") or 0

-- Trigger mode. GW_FRAME only works when the frame number of the thing you
-- want is known in advance, which it never is for a screen you have to play
-- to. With GW_TRIGGER set, the script waits for that file to appear and dumps
-- then -- so the scene is reached first and the capture armed afterwards.
local TRIGGER = os.getenv("GW_TRIGGER")
local function triggerPresent()
  if not TRIGGER then return false end
  local f = io.open(TRIGGER, "r")
  if f then f:close(); return true end
  return false
end

os.execute('mkdir -p "' .. DIR .. '"')

local frame = 0
local dumps = 0

local function dumpRegion(memType, size, path)
  local ok, err = pcall(function()
    local buf = {}
    for i = 0, size - 1 do
      buf[#buf + 1] = string.char(emu.read(i, memType) % 256)
      -- Chunk the concatenation: building one 64K string byte-by-byte with ..
      -- is quadratic and stalls the emulator long enough to trip the script
      -- timeout.
      if #buf >= 4096 then
        local f = io.open(path, "ab")
        f:write(table.concat(buf)); f:close()
        buf = {}
      end
    end
    if #buf > 0 then
      local f = io.open(path, "ab")
      f:write(table.concat(buf)); f:close()
    end
  end)
  if not ok then emu.log("dump failed for " .. path .. ": " .. tostring(err)) end
  return ok
end

local function doDump(tag)
  local base = string.format("%s/%s", DIR, tag)
  os.remove(base .. "_vram.bin")
  os.remove(base .. "_cgram.bin")
  os.remove(base .. "_oam.bin")

  dumpRegion(emu.memType.snesVideoRam, 0x10000, base .. "_vram.bin")
  -- WRAM too: when VRAM differs, the next question is always whether the
  -- bytes were already wrong in the staging buffer the DMA copies from, or
  -- whether the buffer was right and the transfer read it at the wrong
  -- moment. Without this the diff cannot tell those apart.
  os.remove(base .. "_wram.bin")
  dumpRegion(emu.memType.snesWorkRam, 0x20000, base .. "_wram.bin")
  dumpRegion(emu.memType.snesCgRam, 0x200, base .. "_cgram.bin")
  dumpRegion(emu.memType.snesSpriteRam, 0x220, base .. "_oam.bin")

  local png = emu.takeScreenshot()
  if png then
    local f = io.open(base .. "_shot.png", "wb")
    if f then f:write(png); f:close() end
  end

  -- Registers that decide how those bytes are interpreted, so the diff can be
  -- read without going back to the emulator.
  local st = emu.getState()
  local f = io.open(base .. "_state.txt", "w")
  if f then
    local keys = {
      "ppu.bgMode", "ppu.mode1Bg3Priority", "ppu.mainScreenLayers",
      "ppu.subScreenLayers", "ppu.forcedBlank", "ppu.screenBrightness",
      "ppu.colorMathEnabled", "ppu.fixedColor",
      "ppu.layers[0].tilemapAddress", "ppu.layers[0].chrAddress",
      "ppu.layers[0].hscroll", "ppu.layers[0].vscroll",
      "ppu.layers[1].tilemapAddress", "ppu.layers[1].chrAddress",
      "ppu.layers[1].hscroll", "ppu.layers[1].vscroll",
      "ppu.layers[2].tilemapAddress", "ppu.layers[2].chrAddress",
      "ppu.layers[2].hscroll", "ppu.layers[2].vscroll",
      "ppu.oamBaseAddress", "ppu.oamAddressOffset", "ppu.oamMode",
    }
    f:write("frame=" .. frame .. "\n")
    for _, k in ipairs(keys) do
      f:write(k .. "=" .. tostring(st[k]) .. "\n")
    end
    f:close()
  end

  dumps = dumps + 1
  emu.log(string.format("mesen_vram_dump: dumped %s at frame %d", tag, frame))
end

local function onFrame()
  frame = frame + 1
  if TRIGGER then
    -- Stat a few times a second, not every frame; a VRAM dump is expensive
    -- enough that firing it twice would be worse than firing it late.
    if (frame % 12) == 0 and triggerPresent() then
      os.remove(TRIGGER)
      doDump(string.format("f%06d", frame))
    elseif (frame % 1200) == 0 then
      emu.log("mesen_vram_dump: frame " .. frame ..
              " (waiting for trigger " .. TRIGGER .. ")")
    end
    return
  end
  if frame == FRAME then
    doDump(string.format("f%06d", frame))
  elseif EVERY > 0 and frame > FRAME and ((frame - FRAME) % EVERY) == 0 then
    doDump(string.format("f%06d", frame))
  elseif (frame % 1200) == 0 and frame < FRAME then
    emu.log("mesen_vram_dump: frame " .. frame .. " (waiting for " .. FRAME .. ")")
  end
end

emu.addEventCallback(onFrame, emu.eventType.endFrame)

-- Heartbeat file: emu.log() only reaches Mesen's Log Window, which is useless
-- when driving it headlessly. A file on disk is the only way to tell "script
-- never loaded" apart from "script loaded but never reached the frame".
local hb = io.open(DIR .. "/loaded.txt", "w")
if hb then
  if TRIGGER then
    hb:write(string.format("loaded, waiting for trigger %s\n", TRIGGER))
  else
    hb:write(string.format("loaded, target frame %d, every %d\n", FRAME, EVERY))
  end
  hb:close()
end

local hbFrame = 0
emu.addEventCallback(function()
  hbFrame = hbFrame + 1
  if (hbFrame % 300) == 0 then
    local f = io.open(DIR .. "/heartbeat.txt", "w")
    if f then f:write(tostring(hbFrame) .. "\n"); f:close() end
  end
end, emu.eventType.endFrame)

emu.log(string.format("mesen_vram_dump: will dump at frame %d into %s", FRAME, DIR))
