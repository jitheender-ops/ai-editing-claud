# Golden references

`auto-editor-resolve.fcpxml` was produced by `auto-editor --export resolve`
(auto-editor 29.3.1, public domain) from a 10s test clip.

It is here because FCPXML import fidelity is the one risk that cannot be tested
on this machine: Resolve is not scriptable on the free edition, so nothing in
this repo can confirm that Resolve accepts what we write. auto-editor has ~5k
stars and its Resolve export is used daily, so its *element and attribute
vocabulary* is evidence about what Resolve actually accepts.

The test compares vocabulary and structure, not values — our cut points
legitimately differ because the padding defaults differ. What must match is the
shape of the document.

Comparing against it already caught one real bug: we declared `audioChannels=2`,
`audioRate=48000` and `audioLayout="stereo"` for every source, while auto-editor
correctly reported the file's actual mono 44.1kHz. Most screen capture is mono,
so that was not a corner case.
