# MT5 Tick Publisher with NATS & PyQt6

A graphical application for **publishing MetaTrader 5 (MT5) ticks and candlesticks** to a **NATS server** in real-time.  
This tool allows you to scale MT5 data publishing across multiple nodes using **load balancing** and a **pub/sub architecture**.

---

## 🚀 Features

- Publish MT5 ticks to NATS (`ticks.SYMBOL` subjects).
- Supports multiple timeframes: `M1, M5, M10, M15, M30, H1, H2, H4`.
- Monitors MT5 and **auto-reinitializes** if the terminal disconnects.
- **Asynchronous logging** with rotating log files.
- Optional **health check publisher** (periodic system status messages).
- **PyQt6 GUI** for easy configuration and control.
- Designed for **load balancing** with multiple MT5 terminals and NATS nodes.

---

## ⚖️ Load Balancing & Architecture

### The Problem
- MetaTrader 5 terminals are **not inherently concurrent**.  
- Running heavy tick/price feeds for multiple symbols in a single terminal can be unstable or inefficient.  

### The Solution
- This application lets you configure both:
  - **MT5 Terminal Path**
  - **NATS URL**
- You can run **multiple MT5 terminals** (on different servers/VMs or the same machine) and assign each one a set of symbols.
- Each publisher sends its data to NATS.  
- **NATS handles distribution** of published ticks/candles across subscribers.

This architecture enables:
- **Horizontal scaling** – simply add more MT5 terminals and publishers.
- **Load distribution** – spread tick publishing load across several nodes.
- **High availability** – no single point of failure for data publishing.

---

## 📡 Why NATS?

NATS was chosen because it is:
- **Lightweight and fast** – perfect for real-time tick data.
- **Pub/Sub out of the box** – subscribers can consume only the symbols they care about.
- **Scalable** – easy to add more publishers or subscribers.
- **Resilient** – designed for distributed systems and load balancing.

---

## 🖥️ GUI Overview

The PyQt6 GUI provides:
- Input fields for **NATS URL**, **symbols**, and **MT5 credentials**.
- File picker for selecting the **MT5 terminal executable**.
- Options for **log folder** and **log rotation settings**.
- Toggle for **debug logging**.
- Checkbox for enabling **health check publishing**.
- Start/Stop button to control the publisher.
- Live log output with rolling buffer.

---

## 🛠️ Installation

### Prerequisites
- Python 3.9+
- MetaTrader 5 installed
- A running NATS server

### Install dependencies
```bash
pip install -r requirements.txt
requirements.txt should include:

nginx
Copy code
MetaTrader5
nats-py
PyQt6
▶️ Usage
Run the application:

bash
Copy code
python main.py
Enter your NATS URL (e.g., nats://localhost:4222).

Enter a list of symbols (CSV, e.g., BTCUSD,ETHUSD).

Select the MT5 terminal path.

Enter login, password, and server for MT5.

Configure optional logging and health check settings.

Click Start to begin publishing.

📊 Example Architecture Diagram
lua
Copy code
   +----------------+         +---------+         +----------------+
   |  MT5 Terminal  | ----->  | Publisher| ----->  |     NATS       |
   | (Symbols A,B)  |         | (this app)|        |   Cluster      |
   +----------------+         +---------+         +----------------+
           |
   +----------------+         +---------+         +----------------+
   |  MT5 Terminal  | ----->  | Publisher| ----->  |     NATS       |
   | (Symbols C,D)  |         | (this app)|        |   Cluster      |
   +----------------+         +---------+         +----------------+

                     Subscribers consume:
                     ticks.SYMBOL
                     health.mt5publisher
📄 License
MIT License – feel free to use and adapt.

✨ Notes
Use multiple MT5 terminals if you need to handle many symbols concurrently.

Distribute publishers across different NATS nodes for load balancing.

Consumers can subscribe selectively (e.g., only ticks.BTCUSD).