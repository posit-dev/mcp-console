-- Resolve Great Docs links with the inventory generated before the main render.
local file = assert(io.open(quarto.project.directory .. "/python/objects.json"))
local inventory = pandoc.json.decode(file:read("*a")).items
file:close()
local symbols = {}
for _, item in ipairs(inventory) do
  symbols[item.name] = item
end

local input = quarto.doc.input_file:gsub("\\", "/")
local page = input:match("/python/reference/([^/]+)%.qmd$")
local function lookup(name)
  name = name:gsub("%(%)(%s*)$", "")
  if symbols[name] then return symbols[name] end
  if page and symbols["mcp_console." .. page .. "." .. name] then
    return symbols["mcp_console." .. page .. "." .. name]
  end
  return symbols["mcp_console." .. name]
end

local function target(item)
  local dir = pandoc.path.make_relative(
    pandoc.path.directory(quarto.doc.input_file), quarto.project.directory
  )
  local depth = 0
  for part in dir:gsub("\\", "/"):gmatch("[^/]+") do
    if part ~= "." then depth = depth + 1 end
  end
  return string.rep("../", depth) .. item.uri
end

return {{
  traverse = "topdown",
  Link = function(link)
    -- Pandoc URL-encodes the backticks before Lua filters see the target.
    local name = link.target:match("^%%60(.-)%%60$")
    if name then
      local short = name:sub(1, 1) == "~"
      name = name:gsub("^~", "")
      local item = assert(lookup(name), "Unknown Python reference: " .. name)
      link.target = target(item)
      if #link.content == 0 then
        link.content = {pandoc.Code(short and name:match("[^.]+$") or name)}
      end
    end
    -- Don't turn code already inside a link into a nested link.
    return link, false
  end,
  Code = function(code)
    if input:match("/python/") then
      local item = lookup(code.text)
      if item then return pandoc.Link({code}, target(item)), false end
    end
  end
}}
