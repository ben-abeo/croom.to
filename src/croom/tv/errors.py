"""Why the TV could not do what the table asked; the control API answers 409 with the message."""


class TvError(RuntimeError):
    """The TV could not do it: words for the table."""


class TvTimeout(TvError):
    """The TV did not answer in the time given for it: it is off or away, or it is showing its pairing prompt and nobody has
    answered it (the library times out the same way for both, so they cannot be told apart)."""
