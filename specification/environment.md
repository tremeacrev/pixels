# Environment

- Make the experience achievable on any machine through an implementation suited to it.
- There isn't exactly a target terminal, as the remote viewer could vary.
- The implementation should conform to whatever terminal is viewing the program.
- Build each implementation for its target machine. The resulting program need not be portable.
- Choose the rendering backend at implementation time: use a usable GPU when present, or implement
  CPU rendering otherwise. Automatic runtime switching between GPU and CPU is not required.
- Detect the terminal's display capabilities at runtime and adapt presentation to them: use graphics
  where supported, character-based output where needed, and the available color support.
- Count standard-output bytes as accepted only when the underlying write reports them written.
  Bytes still in program-managed buffers or awaiting a write completion report remain pending.
  Acceptance does not confirm that the viewing terminal received, parsed, or displayed the bytes.
- Preserve byte order on standard output, including buffered bytes and bytes in writes awaiting
  completion; bytes from earlier output must not interleave with or follow later output.
- For abandoned output, discard bytes not reported written and not in an underlying write awaiting
  completion, including buffered bytes and unwritten remainders of completed writes; never submit
  or flush them.
- Never wait indefinitely for the terminal, including for an answer to a query or for it to accept
  output, so a slow, silent, or remote terminal cannot hold up the artwork, the response to Ctrl-C
  or the restoration of the terminal.
- Set one finite deadline at the start of sending each frame, before any recovery output it needs.
  Every wait for the terminal to accept frame or recovery output must use only the time remaining
  before that deadline; do not renew it between writes. If the deadline expires before the terminal
  accepts all output for the frame, drop that frame and continue the composition with later frames.
  A timeout does not by itself count as a [failed write](running.md). If part of the frame was
  accepted, follow [Terminal state](terminal-state.md).
- When standard output is a terminal, set one finite deadline at the start of display-capability
  detection. Every wait for the terminal to accept a capability query or for an answer to arrive
  must use only the time remaining before that deadline; do not renew it between queries. A timeout
  while waiting for the terminal to accept a capability query or recovery output does not by itself
  count as a [failed write](running.md). If any display capability remains undetermined when the
  deadline expires, keep running with character-based output and no color rather than failing or
  exiting solely for that reason. A usable answer is a complete, well-formed response to a
  capability query sent during this detection. The response must give a recognized value for
  that capability. Use each usable answer received after expiry to adapt presentation without
  waiting for other answers. If only part of a query was accepted, follow
  [Terminal state](terminal-state.md).
- Treat rendering hardware and terminal display capabilities separately: GPU rendering must still
  produce output the terminal can display.
- Choose visual detail and update rate suited to the target hardware and terminal, keeping motion
  fluid.
- Keep the program's use of the machine bounded as the artwork evolves, so a long run does not peg
  a processor or drain a battery.
