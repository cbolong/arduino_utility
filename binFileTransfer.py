import serial
import time
import os
import serial.tools.list_ports

# USER define
AUTO_DETECT = 1
PORT = "COM19"
BAUD = 115200
CHUNK_SIZE = 4096
FILE_SIZE_SUPPORT = 128 * 1024      # 128 KB
FILE_NAME = "firmware.bin"

# DIR Path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Absolute File Path
ABS_FILE_NAME = os.path.join(BASE_DIR, FILE_NAME)

# MCU/PYTHON Handshake Commands
MCU_ERASE_READY = "ARDUINO_ERASE_READY"
MCU_ERASE_TRIGGER = "ARDUINO_ERASE_TRIGGER"
MCU_READY_TO_START = "ARDUINO_READY_TO_RECEIVED_DATA"
MCU_RECEIVED_LINE_RESPONSE = "ARDUINO_RECEIVED_LINE_DONE"
MCU_TRANSFER_COMPLETED = "ARDUINO_DATA_COMPLETED"
MCU_ERROR = "ARDUINO_ERROR"

PYTHON_DONE = "Press Enter to Exit..."


# Color define
GRAY = "\033[90m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

# Global Variable
chunk_count = 0

os.system('')

if AUTO_DETECT == 1:
    # Arduino Due Programming Port PID might be 003D or 003E
    TARGET_VID = 0x2341
    TARGET_PIDS = 0x003D

    ports = serial.tools.list_ports.comports()

    mcu_device_found = 0
    for port in ports:
        # Check if VID and PID match
        if port.vid == TARGET_VID and port.pid == TARGET_PIDS:
            PORT = port.device
            mcu_device_found = 1
            break

    if mcu_device_found == 1:
        print(f">>>  Arduino Found : {PORT}.")
    else:
        print(f"{RED}>>>  Error: Arduino Device not found.{RESET}")
        input(f"{PYTHON_DONE}")
        exit()


# Check if file exists
if not os.path.isfile(ABS_FILE_NAME):
    print(f"{RED}>>>  Error: {FILE_NAME} not found.{RESET}")
    input(f"{PYTHON_DONE}")
    exit()

# Check file size
file_size = os.path.getsize(ABS_FILE_NAME)
if file_size > FILE_SIZE_SUPPORT:
    # Convert bytes to KB for better readability
    print(f"{RED}>>>  Error: File size too large ({file_size//1024} KB). Must be under {FILE_SIZE_SUPPORT//1024} KB.{RESET}")
    input(f"{PYTHON_DONE}")
    exit()


try:
    with serial.Serial(PORT, BAUD, timeout=0.1) as ser:
        # Wait for MCU_ERASE_READY
        print(f"{GRAY}>>>  handshake wait     : {MCU_ERASE_READY} {RESET}")
        while True:
            if ser.in_waiting > 0:
                line = ser.readline().decode(errors="ignore").strip()

                if line == MCU_ERASE_READY:
                    print(f"{GRAY}>>>  handshake received : {MCU_ERASE_READY} {RESET}")
                    break
                elif line == MCU_ERROR:
                    print(f"{RED}>>>  MCU ERROR!!{RESET}")
                    ser.close()
                    input(f"{PYTHON_DONE}")
                    exit()
                    break
                else:
                    print(f"MCU: {line}")


        # Send MCU_ERASE_TRIGGER
        print(f"{GRAY}>>>  handshake send     : {MCU_ERASE_TRIGGER} {RESET}")
        ser.write(f"{MCU_ERASE_TRIGGER}\n".encode('UTF-8'))



        # Wait for MCU_READY_TO_START
        print(f"{GRAY}>>>  handshake wait     : {MCU_READY_TO_START} {RESET}")
        while True:
            if ser.in_waiting > 0:
                line = ser.readline().decode(errors="ignore").strip()

                if line == MCU_READY_TO_START:
                    print(f"{GRAY}>>>  handshake received : {MCU_READY_TO_START} {RESET}")
                    break
                else:
                    print(f"MCU: {line}")
            time.sleep(0.01)


        # Start file transfer
        with open(ABS_FILE_NAME, "rb") as f:
            print(f">>>  Start to transfer {FILE_NAME} to MCU (Chunk Size : {CHUNK_SIZE} bytes)")
            count = 1
            file_data = f.read()  # Read full file content
            
            # If file is smaller than 128 KB, pad with \x00 until 128 KB
            padding_size = FILE_SIZE_SUPPORT - len(file_data)
            print(f">>>  File size {file_size} bytes. File padded with {padding_size} bytes to reach {FILE_SIZE_SUPPORT//1024} KB.")

            if len(file_data) < FILE_SIZE_SUPPORT:
                file_data += b'\x00' * padding_size

            for i in range(0, len(file_data), CHUNK_SIZE):
                chunk = file_data[i : i + CHUNK_SIZE]

                ser.write(chunk)
                chunk_count += 1
                print(f">>>  Chunk {count:d} sending     | Waiting for MCU response ...")


                # Wait for MCU_RECEIVED_LINE_RESPONSE
                print(f"{GRAY}>>>  handshake wait     : {MCU_RECEIVED_LINE_RESPONSE} {RESET}")
                while True:
                    if ser.in_waiting > 0:
                        line = ser.readline().decode(errors="ignore").strip()

                        if line == MCU_RECEIVED_LINE_RESPONSE:
                            print(f"{GRAY}>>>  handshake received : {MCU_RECEIVED_LINE_RESPONSE} {RESET}")
                            break
                        elif line == MCU_ERROR:
                            print(f"{RED}>>>  MCU ERROR!!{RESET}")
                            ser.close()
                            input(f"{PYTHON_DONE}")
                            exit()
                            break
                        else:
                            print(f"MCU: {line}")
                    time.sleep(0.01)

                count += 1

        print(f">>>  File transfer completed. Total {chunk_count:2d} chunks.")
        
        
        
        # Wait for MCU_TRANSFER_COMPLETED
        print(f"{GRAY}>>>  handshake wait     : {MCU_TRANSFER_COMPLETED} {RESET}")
        while True:
            if ser.in_waiting > 0:
                line = ser.readline().decode(errors="ignore").strip()

                if line == MCU_TRANSFER_COMPLETED:
                    print(f"{GRAY}>>>  handshake received : {MCU_TRANSFER_COMPLETED} {RESET}")
                    break
                else:
                    print(f"MCU: {line}")
            time.sleep(0.01)

        ser.close()

    print(f"{GREEN}>>>  {FILE_NAME} Program Successful.{RESET}")
    input(f"{PYTHON_DONE}")


except FileNotFoundError:
    print(f"{RED}>>>  Error: {FILE_NAME} not found.{RESET}")
    input(f"{PYTHON_DONE}")

except Exception as e:
    print(f"{RED}>>>  Error: {e}.{RESET}")
    input(f"{PYTHON_DONE}")

