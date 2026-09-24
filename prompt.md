# Specification changes

Work in the [specification](specification/) directory to enhance it or fix a problem.

Follow existing conventions, including [style.md](metaspecification/style.md),
[directory.md](metaspecification/directory.md), and [file.md](metaspecification/file.md).

## Prepare version control

- Make sure you are on the main branch.
- Pull changes. Stop if a merge conflict occurs.

## Understand and plan

- Understand the existing specification and metaspecification completely; use subagents.
- Determine the appropriate enhancement, improvement, clarification, or fix.
- Prioritize existing problems and clarifications, but address enhancements or improvements
  immediately if they add great value.
- Consider how to incorporate the change into the specification; use subagents again for planning.
- Look for existing and potential problems that hinder the user's goals or cause technical issues.
  These include contradictions, confusion, pitfalls, overlooked issues, future growing pains, and
  other obstacles.

Remember that the specification is not the final implementation (this is not the target machine).

## Change requirements

Treat cohesion and freedom from contradictions as paramount. Be extremely thorough in preserving
them or fixing existing violations.

Use the following strategies where relevant to the change.

- Use concise, plain language and simple presentation; simplify complex additions.
- Make the smallest complete change, considering all implications and minimizing its impact.
  Larger changes are acceptable when needed to migrate restructured files or directories.
- Prefer removals to additions, without requiring them.

## Review

Use subagents for a thorough adversarial review of the changes against the specification as a whole
and the [change requirements](#change-requirements). Incorporate updates from the review.

## Finish version control

Commit directly to main and push.
