"""Why the TV could not do what the table asked; the control API answers 409 with the message."""


class TvError(RuntimeError):
    """The TV could not do it: words for the table."""
