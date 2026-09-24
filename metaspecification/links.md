# Links and images

## Links

- Write a link as `[label](target)`, keeping its label on one source line. For example,
  [file conventions](file.md) is written as `[file conventions](file.md)`.
- Follow the [directory conventions](directory.md) for repository links and image paths.
- Relative paths start from the containing file; `./` and `../` are supported.

## Heading links

- Link to a heading with its `#anchor`, as in [heading links](#heading-links).
- GitHub derives anchors by trimming outer whitespace, lowercasing letters, replacing spaces
  with hyphens, and removing other whitespace and punctuation. Formatting markup is removed,
  retaining its text.
- Repeated anchors receive increasing suffixes: `-1`, `-2`, and so on.
- Update section links when heading text changes or headings with identical anchors reorder.

## Custom anchors

- Use an HTML anchor such as `<a name="custom-example"></a>` anywhere, even without a heading.
  Choose unique names; a prefix can help avoid ambiguity.
- Link to a custom anchor by its name, using the same `#anchor` form as heading links.
- Custom anchors do not affect heading-anchor naming or numbering.

<a name="custom-example"></a>
This paragraph is the target of [this custom link](#custom-example).

## Images

- To embed an image, put `!` before a link, use bracketed alt text as the label, and put the
  image path in parentheses. Alt text gives a short text equivalent of the image's information.
