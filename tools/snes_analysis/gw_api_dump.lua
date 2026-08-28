local DIR = os.getenv("GW_DIR") or "/tmp/gw_api"
os.execute('mkdir -p "' .. DIR .. '"')
local f = io.open(DIR .. "/api.txt", "w")
for k, v in pairs(emu) do f:write(k .. " = " .. tostring(v) .. "\n") end
f:close()
emu.log("api dumped")
