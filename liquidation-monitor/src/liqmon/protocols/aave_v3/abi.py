"""Aave V3 (v3.7 / POOL_REVISION 11) function and event definitions.

Signatures copied from the verified IPool / IPoolAddressesProvider /
IAaveOracle sources of the deployed implementation.
"""

from ...rpc.abi import Fn, register_errors, topic

# PoolAddressesProvider
GET_POOL = Fn("getPool()", ["address"])
GET_PRICE_ORACLE = Fn("getPriceOracle()", ["address"])
GET_POOL_DATA_PROVIDER = Fn("getPoolDataProvider()", ["address"])
GET_ACL_MANAGER = Fn("getACLManager()", ["address"])
GET_MARKET_ID = Fn("getMarketId()", ["string"])

# Pool
POOL_REVISION = Fn("POOL_REVISION()", ["uint256"])
FLASHLOAN_PREMIUM_TOTAL = Fn("FLASHLOAN_PREMIUM_TOTAL()", ["uint128"])
GET_RESERVES_LIST = Fn("getReservesList()", ["address[]"])
GET_CONFIGURATION = Fn("getConfiguration(address)", ["uint256"])
GET_RESERVE_DATA = Fn(
    "getReserveData(address)",
    [
        "(uint256,uint128,uint128,uint128,uint128,uint128,uint40,uint16,address,address,address,address,uint128,uint128,uint128)"
    ],
)
GET_VIRTUAL_UNDERLYING_BALANCE = Fn("getVirtualUnderlyingBalance(address)", ["uint128"])
GET_LIQUIDATION_GRACE_PERIOD = Fn("getLiquidationGracePeriod(address)", ["uint40"])
GET_USER_ACCOUNT_DATA = Fn(
    "getUserAccountData(address)", ["uint256", "uint256", "uint256", "uint256", "uint256", "uint256"]
)
GET_USER_CONFIGURATION = Fn("getUserConfiguration(address)", ["uint256"])
GET_USER_EMODE = Fn("getUserEMode(address)", ["uint256"])
GET_EMODE_COLLATERAL_CONFIG = Fn("getEModeCategoryCollateralConfig(uint8)", ["(uint16,uint16,uint16)"])
GET_EMODE_COLLATERAL_BITMAP = Fn("getEModeCategoryCollateralBitmap(uint8)", ["uint128"])
GET_LIQUIDATION_LOGIC = Fn("getLiquidationLogic()", ["address"])
LIQUIDATION_CALL = Fn("liquidationCall(address,address,address,uint256,bool)")
FLASH_LOAN_SIMPLE = Fn("flashLoanSimple(address,address,uint256,bytes,uint16)")

# LiquidationLogic library public constants
CLOSE_FACTOR_HF_THRESHOLD = Fn("CLOSE_FACTOR_HF_THRESHOLD()", ["uint256"])
MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD = Fn("MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD()", ["uint256"])
MIN_LEFTOVER_BASE = Fn("MIN_LEFTOVER_BASE()", ["uint256"])

# Oracle
GET_ASSETS_PRICES = Fn("getAssetsPrices(address[])", ["uint256[]"])
GET_ASSET_PRICE = Fn("getAssetPrice(address)", ["uint256"])
BASE_CURRENCY = Fn("BASE_CURRENCY()", ["address"])
BASE_CURRENCY_UNIT = Fn("BASE_CURRENCY_UNIT()", ["uint256"])

# Events
BORROW_TOPIC = topic("Borrow(address,address,address,uint256,uint8,uint256,uint16)")
LIQUIDATION_CALL_TOPIC = topic("LiquidationCall(address,address,address,uint256,uint256,address,bool)")

# Custom errors (protocol/libraries/helpers/Errors.sol, v3.7). Registered so
# simulation reverts are reported by name.
AAVE_ERRORS = [
    "CallerNotPoolAdmin()", "CallerNotPoolOrEmergencyAdmin()", "CallerNotRiskOrPoolAdmin()",
    "CallerNotAssetListingOrPoolAdmin()", "AddressesProviderNotRegistered()", "InvalidAddressesProviderId()",
    "NotContract()", "CallerNotPoolConfigurator()", "CallerNotAToken()", "InvalidAddressesProvider()",
    "InvalidFlashloanExecutorReturn()", "ReserveAlreadyAdded()", "NoMoreReservesAllowed()",
    "EModeCategoryReserved()", "ReserveLiquidityNotZero()", "FlashloanPremiumInvalid()",
    "InvalidReserveParams()", "InvalidEmodeCategoryParams()", "CallerMustBePool()", "InvalidMintAmount()",
    "InvalidBurnAmount()", "InvalidAmount()", "ReserveInactive()", "ReserveFrozen()", "ReservePaused()",
    "BorrowingNotEnabled()", "NotEnoughAvailableUserBalance()", "InvalidInterestRateModeSelected()",
    "HealthFactorLowerThanLiquidationThreshold()", "CollateralCannotCoverNewBorrow()", "NoDebtOfSelectedType()",
    "NoExplicitAmountToRepayOnBehalf()", "UnderlyingBalanceZero()", "HealthFactorNotBelowThreshold()",
    "CollateralCannotBeLiquidated()", "SpecifiedCurrencyNotBorrowedByUser()", "InconsistentFlashloanParams()",
    "BorrowCapExceeded()", "SupplyCapExceeded()", "LtvValidationFailed()", "InconsistentEModeCategory()",
    "ReserveAlreadyInitialized()", "UserHasAssetWithZeroLtv()", "InvalidLtv()", "InvalidLiquidationThreshold()",
    "InvalidLiquidationBonus()", "InvalidDecimals()", "InvalidReserveFactor()", "InvalidBorrowCap()",
    "InvalidSupplyCap()", "InvalidLiquidationProtocolFee()", "InvalidReserveIndex()", "AclAdminCannotBeZero()",
    "InconsistentParamsLength()", "ZeroAddressNotValid()", "InvalidExpiration()", "InvalidSignature()",
    "OperationNotSupported()", "AssetNotListed()", "InvalidOptimalUsageRatio()", "UnderlyingCannotBeRescued()",
    "AddressesProviderAlreadyAdded()", "PoolAddressesDoNotMatch()", "ReserveDebtNotZero()", "FlashloanDisabled()",
    "InvalidMaxRate()", "WithdrawToAToken()", "SupplyToAToken()", "Slope2MustBeGteSlope1()",
    "CallerNotRiskOrPoolOrEmergencyAdmin()", "LiquidationGraceSentinelCheckFailed()", "InvalidGracePeriod()",
    "InvalidFreezeState()", "InvalidLtvzeroState()", "NotBorrowableInEMode()", "CallerNotUmbrella()",
    "ReserveNotInDeficit()", "MustNotLeaveDust()", "UserCannotHaveDebt()", "SelfLiquidation()",
    "CallerNotPositionManager()", "InvalidCollateralInEmode(address,uint256)",
    "InvalidDebtInEmode(address,uint256)", "MustBeEmodeCollateral(address,uint256)",
]  # fmt: skip

EXECUTOR_ERRORS = [
    "NotOwner()", "NotPool()", "BadInitiator()", "UnsupportedSwap(uint8)", "NoCollateralReceived()",
    "InsufficientProfit(uint256,uint256)", "TokenCallFailed(address)",
]  # fmt: skip

register_errors(AAVE_ERRORS + EXECUTOR_ERRORS)
