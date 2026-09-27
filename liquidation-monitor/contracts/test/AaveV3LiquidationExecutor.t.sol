// SPDX-License-Identifier: MIT
pragma solidity 0.8.27;

import {AaveV3LiquidationExecutor} from "../src/AaveV3LiquidationExecutor.sol";

/// Minimal cheatcode interface (no forge-std dependency).
interface Vm {
    function prank(address) external;
    function expectRevert(bytes calldata) external;
}

contract MockERC20 {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

interface IReceiver {
    function executeOperation(address, uint256, uint256, address, bytes calldata) external returns (bool);
}

/// Pool mock: flash loan with 5 bps premium; liquidation repays `debt` and pays
/// `debt * collateralPerDebt / 1e18` collateral (i.e. includes the bonus).
contract MockPool {
    MockERC20 public immutable debt;
    MockERC20 public immutable collateral;
    uint256 public borrowerDebt;
    uint256 public collateralPerDebt;

    constructor(MockERC20 debt_, MockERC20 collateral_, uint256 borrowerDebt_, uint256 collateralPerDebt_) {
        debt = debt_;
        collateral = collateral_;
        borrowerDebt = borrowerDebt_;
        collateralPerDebt = collateralPerDebt_;
    }

    function flashLoanSimple(address receiver, address asset, uint256 amount, bytes calldata params, uint16) external {
        uint256 premium = (amount * 5 + 9_999) / 10_000;
        MockERC20(asset).transfer(receiver, amount);
        require(IReceiver(receiver).executeOperation(asset, amount, premium, msg.sender, params), "exec");
        MockERC20(asset).transferFrom(receiver, address(this), amount + premium);
    }

    function liquidationCall(address, address, address, uint256 debtToCover, bool) external {
        uint256 repay = debtToCover > borrowerDebt ? borrowerDebt : debtToCover;
        borrowerDebt -= repay;
        debt.transferFrom(msg.sender, address(this), repay);
        collateral.transfer(msg.sender, repay * collateralPerDebt / 1e18);
    }
}

/// V2-style router mock paying `rate` out-tokens per in-token (1e18 fixed point).
contract MockV2Router {
    uint256 public rate;

    constructor(uint256 rate_) {
        rate = rate_;
    }

    function swapExactTokensForTokens(uint256 amountIn, uint256 amountOutMin, address[] calldata path, address to, uint256)
        external
        returns (uint256[] memory amounts)
    {
        MockERC20(path[0]).transferFrom(msg.sender, address(this), amountIn);
        uint256 out = amountIn * rate / 1e18;
        require(out >= amountOutMin, "slippage");
        MockERC20(path[path.length - 1]).transfer(to, out);
        amounts = new uint256[](2);
        amounts[0] = amountIn;
        amounts[1] = out;
    }
}

contract AaveV3LiquidationExecutorTest {
    Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    MockERC20 debt;
    MockERC20 collateral;
    MockPool pool;
    MockV2Router router;
    AaveV3LiquidationExecutor executor;
    address constant BORROWER = address(0xB0B);
    address constant STRANGER = address(0xBAD);

    function setUp() public {
        debt = new MockERC20();
        collateral = new MockERC20();
        // Borrower owes 1,000 debt; liquidation pays 1.05 collateral per debt (5% bonus).
        pool = new MockPool(debt, collateral, 1_000e18, 1.05e18);
        debt.mint(address(pool), 1_000_000e18);
        collateral.mint(address(pool), 1_000_000e18);
        // Collateral sells 1:1 for debt on the router.
        router = new MockV2Router(1e18);
        debt.mint(address(router), 1_000_000e18);
        executor = new AaveV3LiquidationExecutor(address(pool), address(this));
    }

    function _params(uint256 minProfit) internal view returns (AaveV3LiquidationExecutor.Params memory p) {
        address[] memory path = new address[](2);
        path[0] = address(collateral);
        path[1] = address(debt);
        p = AaveV3LiquidationExecutor.Params({
            collateralAsset: address(collateral),
            debtAsset: address(debt),
            borrower: BORROWER,
            debtToCover: type(uint256).max,
            swapKind: 2,
            router: address(router),
            path: abi.encode(path),
            minAmountOut: 0,
            minProfit: minProfit
        });
    }

    function test_flashLiquidationKeepsProfit() public {
        executor.executeWithFlashLoan(_params(0), 1_001e18);
        // seized 1,050 collateral -> 1,050 debt; repaid 1,000 + flash premium on 1,001
        uint256 flashAmount = 1_001e18;
        uint256 premium = (flashAmount * 5 + 9_999) / 10_000;
        require(debt.balanceOf(address(executor)) == 1_050e18 - 1_000e18 - premium, "profit");
        require(pool.borrowerDebt() == 0, "debt cleared");
    }

    function test_capitalLiquidation() public {
        debt.mint(address(executor), 1_000e18);
        executor.executeWithCapital(_params(40e18));
        require(debt.balanceOf(address(executor)) == 1_050e18, "capital + profit");
    }

    function test_onlyOwnerCanExecute() public {
        vm.prank(STRANGER);
        vm.expectRevert(abi.encodeWithSelector(AaveV3LiquidationExecutor.NotOwner.selector));
        executor.executeWithFlashLoan(_params(0), 1_001e18);
    }

    function test_callbackRejectsNonPoolCaller() public {
        vm.expectRevert(abi.encodeWithSelector(AaveV3LiquidationExecutor.NotPool.selector));
        executor.executeOperation(address(debt), 1, 0, address(executor), abi.encode(_params(0), uint256(0)));
    }

    function test_callbackRejectsForeignInitiator() public {
        // Anyone can call Pool.flashLoanSimple naming our executor as receiver;
        // the executor must refuse loans it did not initiate.
        vm.prank(STRANGER);
        vm.expectRevert(abi.encodeWithSelector(AaveV3LiquidationExecutor.BadInitiator.selector));
        pool.flashLoanSimple(address(executor), address(debt), 1_001e18, abi.encode(_params(0), uint256(0)), 0);
    }

    function test_unprofitableRouteRevertsAtomically() public {
        router = new MockV2Router(0.9e18); // collateral sells at a 10% loss
        debt.mint(address(router), 1_000_000e18);
        uint256 before = pool.borrowerDebt();
        (bool ok,) = address(executor).call(
            abi.encodeCall(AaveV3LiquidationExecutor.executeWithFlashLoan, (_params(0), 1_001e18))
        );
        require(!ok, "must revert");
        require(pool.borrowerDebt() == before, "state rolled back");
    }

    function test_minProfitEnforced() public {
        (bool ok, bytes memory ret) = address(executor).call(
            abi.encodeCall(AaveV3LiquidationExecutor.executeWithFlashLoan, (_params(1_000e18), 1_001e18))
        );
        require(!ok, "must revert");
        bytes4 sel;
        assembly {
            sel := mload(add(ret, 32))
        }
        require(sel == AaveV3LiquidationExecutor.InsufficientProfit.selector, "InsufficientProfit");
    }

    function test_onlyOwnerCanSweep() public {
        debt.mint(address(executor), 5e18);
        vm.prank(STRANGER);
        vm.expectRevert(abi.encodeWithSelector(AaveV3LiquidationExecutor.NotOwner.selector));
        executor.sweep(address(debt), STRANGER, 5e18);
        executor.sweep(address(debt), address(this), 5e18);
        require(debt.balanceOf(address(this)) == 5e18, "swept");
    }
}
