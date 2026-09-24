# Terminal state

- Draw frames in place in the terminal area, without scrolling or growing scrollback, even when a
  frame covers the area's bottom-right cell, where further output would wrap and scroll the screen.
- Draw the artwork on the terminal's alternate screen where the terminal provides one, so exiting
  restores the screen content and scrollback the user had before the program started.
- Where the terminal provides no alternate screen, include clearing the artwork and leaving the
  cursor at the start of a line in the bounded restoration attempt on exit.
- Minimize flicker.
- Avoid blank or partly drawn frames unless the terminal's output behavior makes them unavoidable.
- If the program continues after the terminal [accepts only part of a frame](environment.md), draw
  every later frame the program sends over the whole terminal area until the terminal accepts a
  complete frame.
- If abandoned output may leave a terminal control or graphics sequence incomplete, recover normal
  command parsing before sending any terminal output other than recovery. Use recovery suited to
  that sequence that is safe to continue or retry after partial acceptance and preserves unrelated
  terminal state. While detection is active and its deadline has not expired, count recovery within
  that deadline. At other times while running, count recovery within the next frame's deadline.
  If a frame deadline expires during recovery, drop that frame and continue recovery under later
  frame deadlines before sending other output. On exit, include pending recovery in the bounded
  restoration attempt.
- While running, after the program establishes its terminal state, the terminal area changes size,
  or a [usable answer received after expiry](environment.md) changes the presentation, draw every
  subsequent frame over the whole terminal area until all output for a whole-area frame started
  after the most recent such event and using the current presentation is accepted. Use the changed
  presentation from the next frame.
- Draw the artwork with the terminal's cursor hidden, so it never appears over the composition.
- Keep what the terminal holds for the artwork bounded as the artwork evolves, so a long run does
  not exhaust or slow the terminal.
- On every exit the program controls, including interruption, a termination signal such as SIGTERM
  or SIGHUP, or error, make a bounded attempt to restore the
  terminal for normal use, undoing changes to cursor visibility, input behavior, or other terminal
  state, and releasing what the terminal holds for the artwork. Set one finite deadline when the
  attempt begins. Every wait during restoration must use only the time remaining before that
  deadline; do not renew it between operations. If the deadline expires, stop the attempt; treat
  incomplete restoration as an error. A failed restoration write is a [failed write](running.md).
- Stop drawing before restoring the terminal and draw nothing afterward, so nothing the program
  has restored is changed again.
