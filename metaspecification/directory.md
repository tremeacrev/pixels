# Directory organization

## Layout

- Organize by subject area. Keep related requirements, examples, and exceptions together.
- Allow at most 10 items directly inside each directory, including the specification root.
  Count files and subdirectories together, including navigation files.
- Keep nesting shallow. Add a directory only to group several related files.
- Files and subdirectories may share a directory. Different subjects may use different layouts.
- Create directories for existing content, not possible future topics.
- Split crowded directories by subtopic, not into arbitrary overflow folders.
- Use shared directories only for topics that apply across subject areas.

## Names

- Use lowercase kebab-case for directory and Markdown file names, such as `error-handling`.
  Choose clear names. Avoid `misc`, numbered parts, and needless repetition of the parent name.

## Navigation

- Store each requirement in one place. Link to it elsewhere instead of repeating it.
- Add `index.md` only when an entry point or reading order is useful. Link rather than repeat.
- Use relative links between files. Update links when moving, renaming, or splitting files.
