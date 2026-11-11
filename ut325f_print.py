import sys
import serial
from serial.tools.list_ports import comports

from ut325f_protocol import find_frame, parse_frame

BAUDRATE = 115200


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ports = [p.device for p in comports()]
    if argv:
        selected_port = argv[0]
    elif ports:
        selected_port = ports[-1]
    else:
        print("No serial ports found. Pass one explicitly: "
              "python ut325f_print.py COM6")
        return 1
    if argv and selected_port not in ports:
        print(f"Warning: {selected_port} not in {ports} - trying anyway.")

    try:
        ser = serial.Serial(selected_port, BAUDRATE, timeout=0.1)
    except Exception as e:
        print(f"Could not open {selected_port}: {e}")
        return 1

    buffer = b''
    print(f"Listening on {selected_port} at {BAUDRATE} baud...")
    try:
        while True:
            buffer += ser.read(1024)
            while True:
                frame, buffer = find_frame(buffer)
                if frame is None:
                    break
                temps = parse_frame(frame)
                out = [f"{t:.2f}" if t is not None else "-" for t in temps]
                print("Temperatures (C):", ", ".join(out))
    except KeyboardInterrupt:
        print("Exiting...")
    finally:
        ser.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
