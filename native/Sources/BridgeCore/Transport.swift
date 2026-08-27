// The receiving end of the socket.
//
// One thread, one blocking `recvfrom`, one callback per frame. A datagram socket
// preserves message boundaries, so there is no buffering or reassembly to do:
// every read either yields exactly one whole frame or nothing.
//
// The read runs at default priority and does no work beyond decoding. Everything
// it hands over lands in the motion core, which is the only thing allowed to be
// slow about it -- and is not.

import Darwin
import Foundation

final class Transport {
    private let path: String
    private let handler: (Frame) -> Void
    private var descriptor: Int32 = -1
    private var thread: Thread?
    private var running = false

    init(path: String, handler: @escaping (Frame) -> Void) {
        self.path = path
        self.handler = handler
    }

    /// Bind the socket, replacing a stale one left by a previous run.
    func start() throws {
        let directory = (path as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(
            atPath: directory, withIntermediateDirectories: true)
        unlink(path)

        descriptor = socket(AF_UNIX, SOCK_DGRAM, 0)
        guard descriptor >= 0 else { throw BridgeError.socket("socket(): \(errno)") }

        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let maxPath = MemoryLayout.size(ofValue: address.sun_path) - 1
        guard path.utf8.count <= maxPath else {
            throw BridgeError.socket("socket path longer than \(maxPath) bytes: \(path)")
        }
        _ = withUnsafeMutablePointer(to: &address.sun_path) { destination in
            path.withCString { source in
                strlcpy(
                    destination.withMemoryRebound(to: CChar.self, capacity: maxPath + 1) { $0 },
                    source, maxPath + 1)
            }
        }

        let size = socklen_t(MemoryLayout<sockaddr_un>.size)
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(descriptor, $0, size) }
        }
        guard bound == 0 else {
            close(descriptor)
            descriptor = -1
            throw BridgeError.socket("bind(\(path)): \(errno)")
        }

        // Room for a burst without dropping frames. Motion is a delta stream, so
        // a lost datagram is a lost millimetre of travel -- cheap, but free to avoid.
        var receiveBuffer: Int32 = 256 * 1024
        setsockopt(
            descriptor, SOL_SOCKET, SO_RCVBUF, &receiveBuffer, socklen_t(MemoryLayout<Int32>.size))

        running = true
        let thread = Thread { [weak self] in self?.loop() }
        thread.name = "bridge-transport"
        thread.qualityOfService = .userInitiated
        self.thread = thread
        thread.start()
    }

    private func loop() {
        var buffer = [UInt8](repeating: 0, count: Frame.byteCount * 4)
        while running {
            let received = buffer.withUnsafeMutableBytes { raw in
                recv(descriptor, raw.baseAddress, raw.count, 0)
            }
            if received < 0 {
                // A closed socket during shutdown is expected; anything else is
                // transient and retried on the next pass.
                if errno == EINTR { continue }
                if !running { return }
                usleep(1000)
                continue
            }
            guard received >= Frame.byteCount else { continue }
            let frame = buffer.withUnsafeBytes { raw in
                Frame(UnsafeRawBufferPointer(rebasing: raw[0..<Frame.byteCount]))
            }
            if let frame { handler(frame) }
        }
    }

    func stop() {
        running = false
        if descriptor >= 0 {
            // Shut the socket down first so the blocked read returns instead of
            // waiting for a datagram that is never coming.
            shutdown(descriptor, SHUT_RDWR)
            close(descriptor)
            descriptor = -1
        }
        unlink(path)
        thread = nil
    }
}

enum BridgeError: Error, CustomStringConvertible {
    case socket(String)

    var description: String {
        switch self {
        case .socket(let detail): return detail
        }
    }
}
