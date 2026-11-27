"""Controller radio selection during calibration."""


def select_reference_mode(restore, board, mode):
    previous = board._resources.read("roles/controller")["mode"]
    if previous != mode:
        restore.callback(board._resources.post, "roles/controller", {"mode": previous})
        board._resources.post("roles/controller", {"mode": mode})


def prepare_local_wifi_references(restore, boards):
    for board in boards:
        if board.api_version[0] >= 5 and board.wifi_rx.get_config()["calib-mode"]:
            select_reference_mode(restore, board, "wifi")


def prepare_packet_reference(restore, board, mode, channel, secondary=0):
    """Tune the dedicated controller for this capture and restore it on exit."""

    def update(write, state, **settings):
        changed = {key: value for key, value in settings.items() if state[key] != value}
        if changed:
            restore.callback(write, {key: state[key] for key in changed})
            write(changed)

    radio = board.wifi_rx if mode == "wifi" else getattr(board, mode)
    config = radio.get_config()
    policy = config["calib-mode" if mode == "wifi" else "reference_mode"]
    if policy == 0:
        raise ValueError(f"{board.host}: enable reference packet generation for {mode} calibration")

    select_reference_mode(restore, board, mode)

    if mode == "wifi":
        # A separate master cannot see the receiving boards' RF switches.
        update(radio.set_reference_config, config, **{"channel-primary": channel, "channel-secondary": secondary, "calib-mode": 2})
    else:
        settings = {"channel": channel}
        if policy == 1:
            settings["rf_switch"] = 1  # Firmware gates the generator on the local REF route.
        if mode == "ble":
            settings["phy"] = "LE1M"
        update(radio.set_config, config, **settings)
        return config["reference_address"]
