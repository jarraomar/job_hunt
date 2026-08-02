"""Application preparation: what to put in the form, never the submitting.

Spec section 3 is absolute and this package is built around it: **no automated
process ever authenticates as Jarra on any job platform.** There is no browser
in the dependency tree, no credential store, and deliberately no `submit`
function anywhere below this line. What this package produces is a prepared
answer set -- the questions a given platform will ask, and the answer to each --
which Jarra pastes, or a future agent hands to him to paste.

"Safe for prefill" therefore means *safe to prepare answers for in advance*, not
"safe to drive". The distinction is in platforms.py.
"""
