// The executable is a shim. Everything it does lives in BridgeCore, so that the
// selection and decoding logic can be reached by tests -- neither is testable
// through a socket, and both have had a real bug in them.

import BridgeCore

runBridge()
