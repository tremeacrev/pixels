# Markdown formatting

These Markdown forms are supported on GitHub; choose those useful for the content. See
[links and images](links.md) for navigation and image syntax.

## Headings and paragraphs

- Start headings with one to six `#` characters and a space; the count sets the level and size.
- Separate paragraphs with a blank line.
- In `.md` files, a source newline normally becomes a space. For a rendered line break, end the
  line with two spaces, a backslash, or `<br/>`.

## Text styles

| Style | Syntax |
| --- | --- |
| Bold | `**text**` or `__text__` |
| Italic | `*text*` or `_text_` |
| Strikethrough | `~~text~~` or `~text~` |
| Bold with nested italic | `**text with _italic_ inside**` |
| All bold and italic | `***text***` |
| Subscript | `<sub>text</sub>` |
| Superscript | `<sup>text</sup>` |
| Underline | `<ins>text</ins>` |

## Quotations and code

- Begin quoted lines with `>`. GitHub indents quotations with a vertical line and gray text.
- Enclose inline code or commands in single backticks; their contents are not formatted.
- Enclose code or text blocks in lines of at least three backticks. Use a longer fence when
  needed to surround literal backticks. For example:

````markdown
```
git status
```
````

## Lists

- Start unordered items with `-`, `*`, or `+` followed by a space.
- Start ordered items with a number, a period, and a space.
- Align a nested item's marker with the first content character of its parent. Repeat this
  alignment at each level; count spaces and the full marker, including multi-digit numbers.

```markdown
100. First item
     - Nested item
       - Further nested item
```

- Use `- [ ]` for an incomplete task and `- [x]` for a completed task.
- Escape an opening parenthesis at the start of a task description:
  `- [ ] \(Optional) Follow up`.

## Footnotes

- Use `[^name]` for a reference and `[^name]:` for its definition. Definitions can appear
  anywhere; rendered footnotes appear at the bottom. GitHub wikis do not support footnotes.
- A footnote may span multiple lines. Indent continuation lines and use a
  [line break](#headings-and-paragraphs) when needed.

```markdown
A statement with a footnote[^note].

[^note]: First line.\
    Second line.
```

## Alerts

- Use alerts only for information crucial to user success, with at most two per article.
- Avoid consecutive alerts.
- Do not nest alerts inside other elements.

Alerts, also called callouts or admonitions, are blockquotes with a type marker on their first
line. GitHub gives each type its own color and icon. For example:

```markdown
> [!NOTE]
> Useful information for readers, including those skimming.
```

| Type | Meaning |
| --- | --- |
| `NOTE` | Useful information, including for readers skimming the content. |
| `TIP` | Advice for doing things better or more easily. |
| `IMPORTANT` | Information needed to achieve the reader's goal. |
| `WARNING` | Urgent information needing attention to avoid problems. |
| `CAUTION` | Risks or negative outcomes of an action. |

## Other inline syntax

- Use emoji codes such as `:smile:` to insert emoji.
- Use HTML comments such as `<!-- hidden text -->` to hide content in rendered Markdown.
- Put a backslash before a Markdown character to display it literally, as in `\*text\*`.
  Escaping does not suppress Markdown formatting in issue or pull request titles.
