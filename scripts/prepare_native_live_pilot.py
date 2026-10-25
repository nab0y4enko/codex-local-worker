#!/usr/bin/env python3
"""Generate an isolated minimal UIKit + XCUITest pilot and native plan (no model calls)."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

APP_ID = "org.codex.localworker.NativePilot"
TEST_ID = APP_ID + "UITests"
PROJECT = """// !$*UTF8*$!
{
  archiveVersion = 1;
  classes = {};
  objectVersion = 56;
  objects = {
    A00000000000000000000001 = {isa = PBXProject; attributes = {LastUpgradeCheck = 2700; TargetAttributes = {A00000000000000000000003 = {CreatedOnToolsVersion = 27.0; }; A00000000000000000000004 = {CreatedOnToolsVersion = 27.0; TestTargetID = A00000000000000000000003; }; }; }; buildConfigurationList = A00000000000000000000010; compatibilityVersion = "Xcode 14.0"; developmentRegion = en; hasScannedForEncodings = 0; mainGroup = A00000000000000000000020; productRefGroup = A00000000000000000000021; projectDirPath = ""; projectRoot = ""; targets = (A00000000000000000000003, A00000000000000000000004, ); };
    A00000000000000000000003 = {isa = PBXNativeTarget; buildConfigurationList = A00000000000000000000011; buildPhases = (A00000000000000000000030, A00000000000000000000031, ); buildRules = (); dependencies = (); name = NativePilot; productName = NativePilot; productReference = A00000000000000000000022; productType = "com.apple.product-type.application"; };
    A00000000000000000000004 = {isa = PBXNativeTarget; buildConfigurationList = A00000000000000000000012; buildPhases = (A00000000000000000000032, A00000000000000000000033, ); buildRules = (); dependencies = (A00000000000000000000040, ); name = NativePilotUITests; productName = NativePilotUITests; productReference = A00000000000000000000023; productType = "com.apple.product-type.bundle.ui-testing"; };
    A00000000000000000000030 = {isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = (A00000000000000000000050, A00000000000000000000053, ); runOnlyForDeploymentPostprocessing = 0; };
    A00000000000000000000031 = {isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0; };
    A00000000000000000000032 = {isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = (A00000000000000000000051, ); runOnlyForDeploymentPostprocessing = 0; };
    A00000000000000000000033 = {isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (A00000000000000000000052, ); runOnlyForDeploymentPostprocessing = 0; };
    A00000000000000000000040 = {isa = PBXTargetDependency; target = A00000000000000000000003; targetProxy = A00000000000000000000041; };
    A00000000000000000000041 = {isa = PBXContainerItemProxy; containerPortal = A00000000000000000000001; proxyType = 1; remoteGlobalIDString = A00000000000000000000003; remoteInfo = NativePilot; };
    A00000000000000000000050 = {isa = PBXBuildFile; fileRef = A00000000000000000000060; };
    A00000000000000000000051 = {isa = PBXBuildFile; fileRef = A00000000000000000000061; };
    A00000000000000000000052 = {isa = PBXBuildFile; fileRef = A00000000000000000000062; };
    A00000000000000000000053 = {isa = PBXBuildFile; fileRef = A00000000000000000000063; };
    A00000000000000000000060 = {isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = AppDelegate.swift; sourceTree = "<group>"; };
    A00000000000000000000061 = {isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = NativePilotUITests.swift; sourceTree = "<group>"; };
    A00000000000000000000062 = {isa = PBXFileReference; lastKnownFileType = wrapper.framework; name = XCTest.framework; path = System/Library/Frameworks/XCTest.framework; sourceTree = SDKROOT; };
    A00000000000000000000063 = {isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = SceneDelegate.swift; sourceTree = "<group>"; };
    A00000000000000000000022 = {isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = NativePilot.app; sourceTree = BUILT_PRODUCTS_DIR; };
    A00000000000000000000023 = {isa = PBXFileReference; explicitFileType = wrapper.cfbundle; includeInIndex = 0; path = NativePilotUITests.xctest; sourceTree = BUILT_PRODUCTS_DIR; };
    A00000000000000000000020 = {isa = PBXGroup; children = (A00000000000000000000060, A00000000000000000000061, A00000000000000000000062, A00000000000000000000063, A00000000000000000000021, ); sourceTree = "<group>"; };
    A00000000000000000000021 = {isa = PBXGroup; children = (A00000000000000000000022, A00000000000000000000023, ); name = Products; sourceTree = "<group>"; };
    A00000000000000000000010 = {isa = XCConfigurationList; buildConfigurations = (A00000000000000000000070, A00000000000000000000071, ); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release; };
    A00000000000000000000011 = {isa = XCConfigurationList; buildConfigurations = (A00000000000000000000072, A00000000000000000000073, ); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release; };
    A00000000000000000000012 = {isa = XCConfigurationList; buildConfigurations = (A00000000000000000000074, A00000000000000000000075, ); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release; };
    A00000000000000000000070 = {isa = XCBuildConfiguration; buildSettings = {IPHONEOS_DEPLOYMENT_TARGET = 17.0; SDKROOT = iphoneos; SWIFT_VERSION = 5.0; }; name = Debug; };
    A00000000000000000000071 = {isa = XCBuildConfiguration; buildSettings = {IPHONEOS_DEPLOYMENT_TARGET = 17.0; SDKROOT = iphoneos; SWIFT_VERSION = 5.0; }; name = Release; };
    A00000000000000000000072 = {isa = XCBuildConfiguration; buildSettings = {GENERATE_INFOPLIST_FILE = NO; INFOPLIST_FILE = Info.plist; PRODUCT_BUNDLE_IDENTIFIER = """ + APP_ID + """; PRODUCT_NAME = NativePilot; SDKROOT = iphoneos; SWIFT_VERSION = 5.0; TARGETED_DEVICE_FAMILY = "1,2"; IPHONEOS_DEPLOYMENT_TARGET = 17.0; }; name = Debug; };
    A00000000000000000000073 = {isa = XCBuildConfiguration; buildSettings = {GENERATE_INFOPLIST_FILE = NO; INFOPLIST_FILE = Info.plist; PRODUCT_BUNDLE_IDENTIFIER = """ + APP_ID + """; PRODUCT_NAME = NativePilot; SDKROOT = iphoneos; SWIFT_VERSION = 5.0; TARGETED_DEVICE_FAMILY = "1,2"; IPHONEOS_DEPLOYMENT_TARGET = 17.0; }; name = Release; };
    A00000000000000000000074 = {isa = XCBuildConfiguration; buildSettings = {GENERATE_INFOPLIST_FILE = YES; PRODUCT_BUNDLE_IDENTIFIER = """ + TEST_ID + """; PRODUCT_NAME = NativePilotUITests; SDKROOT = iphoneos; SWIFT_VERSION = 5.0; TARGETED_DEVICE_FAMILY = "1,2"; IPHONEOS_DEPLOYMENT_TARGET = 17.0; TEST_TARGET_NAME = NativePilot; }; name = Debug; };
    A00000000000000000000075 = {isa = XCBuildConfiguration; buildSettings = {GENERATE_INFOPLIST_FILE = YES; PRODUCT_BUNDLE_IDENTIFIER = """ + TEST_ID + """; PRODUCT_NAME = NativePilotUITests; SDKROOT = iphoneos; SWIFT_VERSION = 5.0; TARGETED_DEVICE_FAMILY = "1,2"; IPHONEOS_DEPLOYMENT_TARGET = 17.0; TEST_TARGET_NAME = NativePilot; }; name = Release; };
  };
  rootObject = A00000000000000000000001;
}
"""
SCHEME = """<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="2700" version="1.7">
 <BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES"><BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="A00000000000000000000003" BuildableName="NativePilot.app" BlueprintName="NativePilot" ReferencedContainer="container:NativePilot.xcodeproj"/></BuildActionEntry></BuildActionEntries></BuildAction>
 <TestAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger" selectedLauncherIdentifier="Xcode.DebuggerFoundation.Launcher" shouldUseLaunchSchemeArgsEnv="YES"><Testables><TestableReference skipped="NO"><BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="A00000000000000000000004" BuildableName="NativePilotUITests.xctest" BlueprintName="NativePilotUITests" ReferencedContainer="container:NativePilot.xcodeproj"/></TestableReference></Testables></TestAction>
 <LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger" selectedLauncherIdentifier="Xcode.DebuggerFoundation.Launcher"/>
 <ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES"/>
 <AnalyzeAction buildConfiguration="Debug"/><ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/>
</Scheme>
"""
APP_SWIFT = '''import UIKit

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     configurationForConnecting connectingSceneSession: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        UISceneConfiguration(name: "Default Configuration", sessionRole: connectingSceneSession.role)
    }
}
'''
SCENE_SWIFT = '''import UIKit

final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?
    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: windowScene)
        let viewController = UIViewController()
        viewController.view.backgroundColor = .systemBackground
        let label = UILabel()
        label.accessibilityIdentifier = "qualification-label"
        label.text = "Before"
        label.font = .systemFont(ofSize: 32, weight: .semibold)
        label.translatesAutoresizingMaskIntoConstraints = false
        viewController.view.addSubview(label)
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: viewController.view.centerXAnchor),
            label.centerYAnchor.constraint(equalTo: viewController.view.centerYAnchor),
        ])
        window.rootViewController = viewController
        window.makeKeyAndVisible()
        self.window = window
    }
}
'''
TEST_SWIFT = '''import XCTest

final class NativePilotUITests: XCTestCase {
    func testAppShowsQualificationLabel() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.staticTexts["qualification-label"].waitForExistence(timeout: 10))
        let image = XCTAttachment(screenshot: app.screenshot())
        image.name = "NativePilotMainScreen"
        image.lifetime = .keepAlways
        add(image)
    }

    func testAppShowsAfter() {
        let app = XCUIApplication()
        app.launch()
        let label = app.staticTexts["qualification-label"]
        XCTAssertTrue(label.waitForExistence(timeout: 10))
        XCTAssertEqual(label.label, "After")
    }
}
'''
INFO = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleExecutable</key><string>$(EXECUTABLE_NAME)</string><key>CFBundleIdentifier</key><string>''' + APP_ID + '''</string><key>CFBundleName</key><string>NativePilot</string><key>CFBundlePackageType</key><string>APPL</string><key>LSRequiresIPhoneOS</key><true/><key>UIApplicationSceneManifest</key><dict><key>UIApplicationSupportsMultipleScenes</key><false/><key>UISceneConfigurations</key><dict><key>UIWindowSceneSessionRoleApplication</key><array><dict><key>UISceneConfigurationName</key><string>Default Configuration</string><key>UISceneDelegateClassName</key><string>$(PRODUCT_MODULE_NAME).SceneDelegate</string></dict></array></dict></dict><key>UILaunchScreen</key><dict/></dict></plist>
'''


def run(argv, *, cwd=None):
    subprocess.run([str(x) for x in argv], cwd=str(cwd) if cwd else None, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--simulator-udid", required=True)
    args = parser.parse_args()
    root = Path(args.root).expanduser()
    if not root.is_absolute() or root.exists():
        raise SystemExit("--root must be absolute and not exist")
    root.mkdir(parents=True)
    repo = root / "repo"
    repo.mkdir()
    project = repo / "NativePilot.xcodeproj"
    (project / "xcshareddata" / "xcschemes").mkdir(parents=True)
    (project / "project.pbxproj").write_text(PROJECT, encoding="utf-8")
    (project / "xcshareddata" / "xcschemes" / "NativePilot.xcscheme").write_text(SCHEME, encoding="utf-8")
    (repo / "AppDelegate.swift").write_text(APP_SWIFT, encoding="utf-8")
    (repo / "SceneDelegate.swift").write_text(SCENE_SWIFT, encoding="utf-8")
    (repo / "NativePilotUITests.swift").write_text(TEST_SWIFT, encoding="utf-8")
    (repo / "Info.plist").write_text(INFO, encoding="utf-8")
    (repo / ".gitignore").write_text("DerivedData/\n*.xcresult/\n", encoding="utf-8")
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repo)
    run(["git", "-c", "user.name=Task26 Native Pilot", "-c", "user.email=task26-native@localhost", "add", "-A"], cwd=repo)
    run(["git", "-c", "user.name=Task26 Native Pilot", "-c", "user.email=task26-native@localhost", "commit", "-m", "native pilot baseline"], cwd=repo)
    task = {
        "task_id": "task26-native-write", "kind": "write", "repo_path": str(repo),
        "base_ref": "main", "goal": "Change the app's qualification label text from Before to After in SceneDelegate.swift only.",
        "acceptance_criteria": ["The qualification label text is After",
                                "NativePilotUITests/testAppShowsAfter passes"],
        "allowed_paths": ["SceneDelegate.swift"],
        "timeout_seconds": 1800, "max_attempts": 1, "workstream": "task26-native",
        "test_commands": [["/usr/bin/xcodebuild", "-project", "NativePilot.xcodeproj", "-scheme", "NativePilot", "-sdk", "iphonesimulator", "-destination", "id=" + args.simulator_udid, "-derivedDataPath", "DerivedData", "test", "-only-testing:NativePilotUITests/NativePilotUITests/testAppShowsAfter"]],
        "native_plan": str(root / "native-plan.json"), "ui_review": True,
    }
    (root / "task.json").write_text(json.dumps(task, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    derived = "{report_dir}/apple-scratch/DerivedData"
    app = derived + "/Build/Products/Debug-iphonesimulator/NativePilot.app"
    result = "{report_dir}/apple-scratch/native-preflight.xcresult"
    plan = {
        "version": 2, "destination": {"simulator": args.simulator_udid},
        "build": ["/usr/bin/xcodebuild", "-project", "{worktree}/NativePilot.xcodeproj", "-scheme", "NativePilot", "-configuration", "Debug", "-sdk", "iphonesimulator", "-derivedDataPath", derived, "-destination", "id={simulator_udid}", "build"],
        "install": ["/usr/bin/xcrun", "simctl", "install", "{simulator_udid}", app],
        "launch": ["/usr/bin/xcrun", "simctl", "launch", "{simulator_udid}", APP_ID],
        "test": ["/usr/bin/xcodebuild", "-project", "{worktree}/NativePilot.xcodeproj", "-scheme", "NativePilot", "-configuration", "Debug", "-sdk", "iphonesimulator", "-derivedDataPath", derived, "-destination", "id={simulator_udid}", "-resultBundlePath", result, "test", "-only-testing:NativePilotUITests/NativePilotUITests/testAppShowsQualificationLabel"],
        "app_bundle": app, "result_bundle": result, "bundle_id": APP_ID,
        "screenshots": [
            {"name": "main-screen", "settle_seconds": 2},
            {"name": "xctest-main-screen", "xcuitest_attachment": "NativePilotMainScreen"},
        ],
        "capture_sources": [{"name": "reference", "repo_path": str(repo), "ref": "main"}],
    }
    (root / "native-plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"root": str(root), "repo": str(repo), "task": str(root / "task.json"),
                      "native_plan": str(root / "native-plan.json"), "simulator_udid": args.simulator_udid}, sort_keys=True))


if __name__ == "__main__":
    main()
