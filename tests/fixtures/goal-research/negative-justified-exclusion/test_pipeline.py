from pipeline import Reading, encode_reading, latest_value


def main() -> None:
    readings = [
        Reading("device-a", 1, 2.0, "first"),
        Reading("device-a", 2, 3.0, "second"),
    ]
    assert encode_reading(readings[0]) == ("device-a", 1, 2.0)
    assert latest_value(readings) == 3.0
    print("ordinary negative fixture tests passed")


if __name__ == "__main__":
    main()
