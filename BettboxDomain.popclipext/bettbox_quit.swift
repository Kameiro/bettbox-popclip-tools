import AppKit
import Foundation

let applications = NSRunningApplication.runningApplications(
    withBundleIdentifier: "com.appshub.bettbox"
)

guard let application = applications.first else {
    exit(0)
}

exit(application.terminate() ? 0 : 1)
