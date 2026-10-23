-- Keep the Markdown guides readable on GitHub without website front matter.
function Pandoc(doc)
  local heading = doc.blocks[1]
  if quarto.doc.input_file:match("%.md$") and heading and heading.t == "Header" and heading.level == 1 then
    doc.meta.title = pandoc.MetaInlines(heading.content)
    doc.meta.pagetitle = pandoc.MetaString(pandoc.utils.stringify(heading.content))
    doc.blocks:remove(1)
  end
  return doc
end

-- Wide reference tables scroll within the page and remain keyboard-accessible.
function Table(tbl)
  return pandoc.Div({tbl}, pandoc.Attr("", {"table-responsive"}, {
    tabindex = "0",
    role = "region",
    ["aria-label"] = "Scrollable table"
  }))
end

-- Pandoc make_relative removes a directory prefix; it does not add ".." for siblings.
local function package_link(path)
  local input_dir = pandoc.path.directory(quarto.doc.input_file)
  local relative_dir = pandoc.path.make_relative(input_dir, quarto.project.directory)
  local depth = 0
  for component in relative_dir:gsub("\\", "/"):gmatch("[^/]+") do
    if component ~= "." then depth = depth + 1 end
  end
  return string.rep("../", depth) .. path
end

-- Keep public package URLs usable on GitHub and local in the rendered website.
function Link(link)
  local public_prefix = "https://posit-dev.github.io/mcp-console/"
  if link.target:sub(1, #public_prefix) == public_prefix then
    local path, suffix = link.target:sub(#public_prefix + 1):match("^([^#?]+)(.*)$")
    if path and (path:match("^python/") or path:match("^r/")) then
      link.target = package_link(path) .. suffix
      return link
    end
  end
  local path, suffix = link.target:match("^([^#?]+)(.*)$")
  if path and not path:match("^[/#]") and not path:match("^%a[%w+.-]*:") then
    local input_dir = pandoc.path.directory(quarto.doc.input_file)
    local target = pandoc.path.normalize(pandoc.path.join({input_dir, path}))
    local root = pandoc.path.directory(quarto.project.directory)
    -- Pandoc uses host separators and retains parent-directory components.
    -- Classify repository paths and construct URLs with forward slashes on every host.
    local source = pandoc.path.make_relative(target, root):gsub("\\", "/")
    while source:match("[^/]+/%.%./") do
      source = source:gsub("[^/]+/%.%./", "")
    end
    if source == "r/README.md" then
      link.target = package_link("r/index.html") .. suffix
    elseif not source:match("^docs/") or source:match("^docs/templates/") then
      link.target = "https://github.com/posit-dev/mcp-console/blob/main/" .. source .. suffix
    end
  end
  return link
end
