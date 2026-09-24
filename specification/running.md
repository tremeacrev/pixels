# Running

- Install the program as `pixels`, available on the command line.
- Running `pixels` immediately starts the artwork inside the terminal itself.
- Provide no configuration, command-line options, menus, or interactive controls of its own.
- Keep running when standard input is not a terminal or has reached its end.
- Draw the artwork on standard output, the stream connected to the terminal viewing the program.
- If standard output is not a terminal, treat this as an error. Leave it empty and attempt to
  print a message on standard error. Set one finite deadline when the attempt begins; do not
  renew it. Every wait to write or flush the message must use only the time remaining before
  that deadline. Stop the attempt when the message is fully written and flushed, a write or
  flush fails, or the deadline expires. Do not write or flush the message after the attempt ends.
  Then exit with a nonzero status.
- Exit with a nonzero status when writing to standard output fails, such as when the terminal
  viewing the program disconnects.
- Exit with a nonzero status when the program ends because of an error, so failure is not reported
  as success.
