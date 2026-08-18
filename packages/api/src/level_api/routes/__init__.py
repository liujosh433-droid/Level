"""HTTP routes for Level.

Convention: one file per top-level noun. Every mutating handler takes
`store: UserStore = Depends(get_user_store)` so auth is enforced at the
dependency layer.
"""
