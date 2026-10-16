public with sharing class CleanFormatter {
    public static String money(Decimal amount) {
        return amount == null ? '' : '$' + amount.setScale(2).format();
    }
}
