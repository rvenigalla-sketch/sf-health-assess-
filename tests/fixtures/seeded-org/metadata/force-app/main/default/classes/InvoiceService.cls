public class InvoiceService {
    public static void recalculate(List<Invoice__c> invoices) {
        for (Invoice__c inv : invoices) {
            List<Invoice__c> siblings = [SELECT Id, Amount__c FROM Invoice__c WHERE Status__c = :inv.Status__c];
            inv.Amount_Total__c = 0;
            for (Invoice__c s : siblings) {
                inv.Amount_Total__c += s.Amount__c;
            }
            try {
                update new Invoice__c(Id = inv.Id, Owner_Queue__c = '00G5e000003XyZaEAK');
            } catch (Exception e) {
            }
        }
    }
}
