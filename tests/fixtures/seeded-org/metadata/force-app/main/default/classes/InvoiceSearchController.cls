public without sharing class InvoiceSearchController {
    @AuraEnabled
    public static List<Invoice__c> search(String term) {
        String q = 'SELECT Id, Amount__c FROM Invoice__c WHERE Name LIKE \'%' + term + '%\'';
        return Database.query(q);
    }
}
