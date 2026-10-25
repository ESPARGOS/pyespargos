# Custom demos

Keep personal demos and local experiments here. Everything in this folder is
ignored by Git except this README and `.gitignore`.

Put each demo in its own subfolder. From there, shared QML components are in
`../../common`. Import Python helpers from `demos.common.application`,
`demos.wifi.common.application`, or `demos.iq.common.application` after adding
the repository root to `sys.path`, as the published demos do.
