package main
import (
	"net/http"
	"github.com/gin-gonic/gin"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"sync"
	"github.com/IBM/sarama"
)
type KafkaMessage struct {
	EventID string `json:"event_id"`
	Normal map[string]string `json:"normal"`
	CsvFile string `json:"csv_file"`
	Source string `json:"source"`
}
type Broker struct{
	clients map[chan KafkaMessage]bool
	mu sync.RWMutex
}
func NewBroker() *Broker{
	return &Broker{clients:make(map[chan KafkaMessage]bool),}
}
func (b *Broker) Subscribe() chan KafkaMessage {
	b.mu.Lock()
	defer b.mu.Unlock()
	ch := make(chan KafkaMessage, 10)
	b.clients[ch] = true
	return ch
}
func (b *Broker) Unsubscribe(ch chan KafkaMessage) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.clients, ch)
	close(ch)
}
func (b *Broker) Broadcast(msg KafkaMessage) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	for ch := range b.clients {
		select {
		case ch <- msg:
		default:
		}
	}
}
func startKafkaConsumer(brokers []string, topic string, broker *Broker) {
	config := sarama.NewConfig()
	config.Consumer.Return.Errors = true
	consumer, err := sarama.NewConsumer(brokers, config)
	if err != nil {
		log.Fatalf("Error creating Kafka consumer: %v", err)
	}
	partitionConsumer, err := consumer.ConsumePartition(topic, 0, sarama.OffsetNewest)
	if err != nil {
		log.Fatalf("Error starting partition consumer: %v", err)
	}
	go func() {
		defer consumer.Close()
		defer partitionConsumer.Close()
		for msg := range partitionConsumer.Messages() {
			var kMsg KafkaMessage
			if err := json.Unmarshal(msg.Value, &kMsg); err != nil {
				log.Printf("Failed to decode JSON: %v", err)
				continue
			}
			broker.Broadcast(kMsg)
		}
	}()
}
func main(){
	router := gin.Default()
	broker := NewBroker()
	kafkaBrokers := []string{"localhost:9092"}
	startKafkaConsumer(kafkaBrokers, "extract-web", broker)
	router.GET("/", rootLevel)
	router.GET("/events", func(c *gin.Context) {
		messageChan := broker.Subscribe()
		defer broker.Unsubscribe(messageChan)
		c.Stream(func(w io.Writer) bool {
			select {
			case msg, ok := <-messageChan:
				if !ok {
					return false
				}
				data, err := json.Marshal(msg)
				if err != nil {
					return true
				}
				c.SSEvent("message", string(data))
				return true
			case <-c.Request.Context().Done():
				return false
			}
		})
	})
	router.Run()
}
func rootLevel(c *gin.Context){
	c.IndentedJSON(http.StatusOK, gin.H{"message" : "Running"})
}

