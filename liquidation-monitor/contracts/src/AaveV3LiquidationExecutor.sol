// SPDX-License-Identifier: MIT
pragma solidity 0.8.27;

interface IERC20Minimal {
    function balanceOf(address account) external view returns (uint256);
}

interface IAaveV3Pool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;

    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address borrower,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
}

/// @dev Uniswap SwapRouter02 / PancakeSwap SmartRouter V3 exactInput (no deadline field).
interface IV3SwapRouter {
    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }

    function exactInput(ExactInputParams calldata params) external payable returns (uint256 amountOut);
}

interface IV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

/// @title AaveV3LiquidationExecutor
/// @notice Research executor for Aave V3 liquidations:
///         [flash-borrow debt asset] -> Pool.liquidationCall -> swap seized collateral -> repay -> keep profit.
/// @dev Uses only public, documented entry points (Pool.flashLoanSimple, Pool.liquidationCall,
///      public DEX routers). Every path is owner-gated, and the flash-loan callback only accepts
///      loans that this contract itself initiated. The whole sequence is atomic: any shortfall
///      reverts the transaction.
contract AaveV3LiquidationExecutor {
    uint8 public constant SWAP_NONE = 0;
    uint8 public constant SWAP_V3_ROUTER = 1;
    uint8 public constant SWAP_V2_ROUTER = 2;

    struct Params {
        address collateralAsset;
        address debtAsset;
        address borrower;
        uint256 debtToCover; // passed to liquidationCall; type(uint256).max lets the Pool clamp it
        uint8 swapKind;
        address router;
        bytes path; // V3: packed path; V2: abi.encode(address[])
        uint256 minAmountOut; // swap slippage bound, debt-asset units
        uint256 minProfit; // debt-asset units that must remain after repaying the loan
    }

    address public immutable owner;
    IAaveV3Pool public immutable pool;

    event LiquidationExecuted(
        address indexed borrower,
        address indexed collateralAsset,
        address indexed debtAsset,
        uint256 collateralReceived,
        uint256 profit
    );

    error NotOwner();
    error NotPool();
    error BadInitiator();
    error UnsupportedSwap(uint8 kind);
    error NoCollateralReceived();
    error InsufficientProfit(uint256 balance, uint256 required);
    error TokenCallFailed(address token);

    constructor(address pool_, address owner_) {
        pool = IAaveV3Pool(pool_);
        owner = owner_;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // --------------------------------------------------------------- entry points

    /// @notice Liquidate funded by an Aave flash loan of `flashAmount` debt asset.
    function executeWithFlashLoan(Params calldata p, uint256 flashAmount) external onlyOwner {
        uint256 startBalance = IERC20Minimal(p.debtAsset).balanceOf(address(this));
        pool.flashLoanSimple(address(this), p.debtAsset, flashAmount, abi.encode(p, startBalance), 0);
    }

    /// @notice Liquidate with debt-asset capital already held by this contract.
    function executeWithCapital(Params calldata p) external onlyOwner {
        uint256 startBalance = IERC20Minimal(p.debtAsset).balanceOf(address(this));
        uint256 received = _liquidateAndSwap(p);
        uint256 endBalance = IERC20Minimal(p.debtAsset).balanceOf(address(this));
        uint256 required = startBalance + p.minProfit;
        if (endBalance < required) revert InsufficientProfit(endBalance, required);
        emit LiquidationExecuted(p.borrower, p.collateralAsset, p.debtAsset, received, endBalance - startBalance);
    }

    /// @notice Aave IFlashLoanSimpleReceiver callback.
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        if (msg.sender != address(pool)) revert NotPool();
        if (initiator != address(this)) revert BadInitiator();
        (Params memory p, uint256 startBalance) = abi.decode(params, (Params, uint256));

        uint256 received = _liquidateAndSwap(p);

        uint256 owed = amount + premium;
        uint256 balance = IERC20Minimal(asset).balanceOf(address(this));
        uint256 required = startBalance + owed + p.minProfit;
        if (balance < required) revert InsufficientProfit(balance, required);

        _forceApprove(asset, address(pool), owed);
        emit LiquidationExecuted(p.borrower, p.collateralAsset, p.debtAsset, received, balance - startBalance - owed);
        return true;
    }

    // ------------------------------------------------------------------ admin

    function sweep(address token, address to, uint256 amount) external onlyOwner {
        _callToken(token, abi.encodeWithSelector(0xa9059cbb, to, amount));
    }

    // --------------------------------------------------------------- internals

    function _liquidateAndSwap(Params memory p) internal returns (uint256 collateralReceived) {
        bool sameAsset = p.collateralAsset == p.debtAsset;
        uint256 collateralBefore = sameAsset ? 0 : IERC20Minimal(p.collateralAsset).balanceOf(address(this));

        uint256 debtBalance = IERC20Minimal(p.debtAsset).balanceOf(address(this));
        _forceApprove(p.debtAsset, address(pool), debtBalance);
        pool.liquidationCall(p.collateralAsset, p.debtAsset, p.borrower, p.debtToCover, false);
        _forceApprove(p.debtAsset, address(pool), 0);

        if (sameAsset) return 0;

        collateralReceived = IERC20Minimal(p.collateralAsset).balanceOf(address(this)) - collateralBefore;
        if (collateralReceived == 0) revert NoCollateralReceived();
        if (p.swapKind == SWAP_NONE) return collateralReceived;

        _forceApprove(p.collateralAsset, p.router, collateralReceived);
        if (p.swapKind == SWAP_V3_ROUTER) {
            IV3SwapRouter(p.router).exactInput(
                IV3SwapRouter.ExactInputParams({
                    path: p.path,
                    recipient: address(this),
                    amountIn: collateralReceived,
                    amountOutMinimum: p.minAmountOut
                })
            );
        } else if (p.swapKind == SWAP_V2_ROUTER) {
            address[] memory route = abi.decode(p.path, (address[]));
            IV2Router(p.router).swapExactTokensForTokens(
                collateralReceived, p.minAmountOut, route, address(this), block.timestamp
            );
        } else {
            revert UnsupportedSwap(p.swapKind);
        }
        _forceApprove(p.collateralAsset, p.router, 0);
    }

    /// @dev approve that tolerates tokens without a bool return and tokens that
    ///      require resetting the allowance to zero first.
    function _forceApprove(address token, address spender, uint256 amount) internal {
        bytes memory data = abi.encodeWithSelector(0x095ea7b3, spender, amount);
        if (!_tryCall(token, data)) {
            _callToken(token, abi.encodeWithSelector(0x095ea7b3, spender, 0));
            _callToken(token, data);
        }
    }

    function _tryCall(address token, bytes memory data) private returns (bool) {
        (bool ok, bytes memory ret) = token.call(data);
        return ok && (ret.length == 0 || abi.decode(ret, (bool))) && token.code.length > 0;
    }

    function _callToken(address token, bytes memory data) private {
        if (!_tryCall(token, data)) revert TokenCallFailed(token);
    }
}
