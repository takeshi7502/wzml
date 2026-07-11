def get_version() -> str:
    """
    Returns the version details. Do not Interfere with this !

    :return: The version details in the format 'vMAJOR.MINOR.PATCH-STATE'
    :rtype: str
    """
    MAJOR = "3"
    MINOR = "1"
    PATCH = "7"
    STATE = "3"
    return f"v{MAJOR}.{MINOR}.{PATCH}-{STATE}"


if __name__ == "__main__":
    print(get_version())
