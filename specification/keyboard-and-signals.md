# Keyboard and signals

- Read keyboard input from the terminal viewing the program, whether or not standard input is that
  terminal.
- Ignore keyboard input other than Ctrl-C, so keystrokes do not appear over the artwork.
- Consume the terminal's answers to the program's queries while running.
- As part of the bounded [restoration attempt](terminal-state.md), discard input already available
  from the viewing terminal, including bytes buffered by the program, recording any Ctrl-C for
  deferred handling. Do this before restoring normal input behavior, without waiting for more input;
  input other than Ctrl-C arriving after this discard may reach the shell.
- Keep keys other than Ctrl-C from ending the program or freezing what is on screen,
  including keys the terminal turns into a signal or a pause of output.
- Never let waiting for keyboard input pause or slow the artwork, however long no key is pressed.
- Never let drawing a frame delay the response to Ctrl-C, however long the frame takes.
- End the program on Ctrl-C, whether it arrives as terminal input or as an interruption signal.
- Finish the bounded [restoration attempt](terminal-state.md) before acting on any Ctrl-C or other
  catchable signal that arrives during restoration, so a repeated key cannot interrupt it.
- Exit with a success status on Ctrl-C, unless writing to standard output fails, the program ends
  because of an error, or SIGTERM or SIGHUP is also received.
- Exit with a nonzero status if SIGTERM or SIGHUP is received, even if Ctrl-C is also received.
  Count either signal pending for deferred handling during restoration as received.
