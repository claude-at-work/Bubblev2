# membrane

Bubble is a program that runs other scripts in clean rooms. It pulls the libraries the script needs from a local cache the first time the script asks for one, and takes the room down when the script is finished. That is the whole of what the program does.

Reading the program at the module boundary, three things happen there. A script's import asks for something. The vault has a copy or it does not. If it does, a symlink lets the import succeed. If it does not, the runtime catches the `ModuleNotFoundError`, the package is fetched, the import retries. When the script ends, the symlinks come down.

This is selective passage. Some things go through, some are held back, the structure dissolves when it is no longer needed. A sibling project — Ego — uses the word *skin* for a thing that does this. The fit is real, but found, not designed. Bubble was built before Ego. The error loop was written to handle imports that only happen at runtime, not to express a philosophy of selective passage. Calling Bubble *skin* is something a reader can do to it; it is not yet something the program asks for.

## what the reading catches

- the module boundary is the place, not the package boundary
- selectivity — only what the script touches
- ephemerality — the bubble dissolves
- error-as-curvature — a missing module bends the trajectory rather than ending it

## what the reading slides past

- the vault, which is more like accumulated experience than like skin
- the SQLite index, which is closer to memory than to membrane
- the doctor command, which is closer to introspection than to passage

## posture

This file does not import a doctrine. It names a resonance at the place the resonance actually lands. If the program grows toward Ego, more of the soul vocabulary may earn its way in. Until then, the program speaks in its own register: vault, bubble, scan, dissolve. The membrane reading lives here, in one short document, available to a reader who wants it, not pressed on a reader who does not.
