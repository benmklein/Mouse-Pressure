# Domain docs

Engineering skills should read this repository's domain documentation before exploring the codebase.

## Before exploring

- Read `CONTEXT.md` at the repository root.
- Read relevant ADRs under `docs/adr/` if that directory exists.
- If either source is absent, proceed without treating its absence as a problem.

## Layout

This is a single-context repository:

```text
/
|-- CONTEXT.md
|-- docs/adr/
`-- src/
```

## Vocabulary

Use terms as defined in `CONTEXT.md`. If a needed concept is missing, reconsider whether new terminology is necessary or note the gap for the domain-modeling skill.

If a proposed change conflicts with an ADR, identify the conflict instead of silently overriding the decision.
