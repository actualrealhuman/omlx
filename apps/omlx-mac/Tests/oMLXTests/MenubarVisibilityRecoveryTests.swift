import XCTest
@testable import oMLX

final class MenubarVisibilityRecoveryTests: XCTestCase {
    func testVisibleIconDoesNotRecreate() {
        var state = MenubarVisibilityRecoveryState()

        XCTAssertEqual(state.observe(isHidden: false), .visible)
        XCTAssertFalse(state.didRecreate)
    }

    func testPersistentlyHiddenIconGetsOneRecreateThenRemainsHidden() {
        var state = MenubarVisibilityRecoveryState()

        XCTAssertEqual(state.observe(isHidden: true), .recreate)
        XCTAssertTrue(state.didRecreate)
        XCTAssertEqual(state.observe(isHidden: true), .remainsHidden)
        XCTAssertEqual(state.observe(isHidden: true), .remainsHidden)
    }

    func testIconVisibleAfterRecreateCompletesWithoutAnotherRecoveryAction() {
        var state = MenubarVisibilityRecoveryState()

        XCTAssertEqual(state.observe(isHidden: true), .recreate)
        XCTAssertEqual(state.observe(isHidden: false), .visible)
        XCTAssertTrue(state.didRecreate)
    }
}
