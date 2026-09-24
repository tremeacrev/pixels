# Composition

- Show an abstract composition in flowing motion rather than a display of text, using several
  shapes when the terminal area can show them distinctly and color when the display capabilities
  allow.
- Keep the artwork evolving until the user or the system ends the program.
- Follow the [artistic direction](artistic-direction.md) as form, color, and motion evolve.
- Keep the program's memory use bounded as the artwork evolves, so growth never causes a system
  kill that leaves the terminal unrestored.
- Use the available terminal area when both reported dimensions are positive; otherwise, fall back
  to 80 by 24 cells.
- Continue the same composition across terminal resizes, adapting it to the new area before
  drawing the next frame, including a resize that arrives while the program is drawing.
- Keep each shape's proportions on the terminal's character grid, using the cell width and height
  the terminal reports when both are available and positive. Otherwise, use a cell about twice as
  tall as it is wide, so a round form does not appear as a stretched ellipse.
- Stay usable at any terminal size, including a single cell, rather than failing or exiting.
