@IsTest(SeeAllData=true)
private class InvoiceServiceTest {
    @IsTest
    static void runsWithoutAsserting() {
        Invoice__c inv = new Invoice__c(Amount__c = 10, Status__c = 'Open');
        insert inv;
        InvoiceService.recalculate(new List<Invoice__c>{ inv });
    }
}
